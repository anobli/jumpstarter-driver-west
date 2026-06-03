# Copyright (c) 2026 BayLibre
# SPDX-License-Identifier: Apache-2.0

import asyncio
import asyncio.subprocess
import hashlib
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncGenerator

from anyio.streams.file import FileReadStream, FileWriteStream

from jumpstarter.driver import Driver, export


@dataclass(kw_only=True)
class West(Driver):
    """West tool driver for remote Zephyr RTOS development

    This driver enables using standard Zephyr tools (west, twister) from your local machine
    to interact with hardware on a remote exporter. You can flash firmware, debug, and run
    tests on boards located elsewhere while using familiar Zephyr workflows.

    The driver manages multiple Zephyr workspaces on the exporter side, allowing you to:
    - Initialize and update Zephyr workspaces for different versions/branches
    - Install the Zephyr SDK
    - Flash firmware to remote boards
    - Run twister tests on remote hardware
    - Isolate workspaces by client/user to prevent conflicts
    """

    workspace_base: str
    """Base directory for all West workspaces on the exporter (required).

    Individual workspaces are created as subdirectories based on manifest URL,
    revision, and optional client ID. Each workspace is isolated with its own
    Python virtual environment.
    Example: '/home/exporter/jumpstarter-workspaces'
    """

    sdk_path: str | None = None
    """Path to the Zephyr SDK installation on the exporter (optional).

    Can be installed via the install_sdk command. If not specified and needed,
    you'll be prompted to run install_sdk first.
    Example: '/opt/zephyr-sdk-0.16.5'
    """

    west_path: str = "west"
    """Path to the west executable (default: 'west')"""

    hardware_map: str | None = None
    """Path to the hardware map YAML file on the exporter (required for twister).

    The hardware map describes the connected boards and their serial ports.
    Example: '/home/exporter/hardware-map.yaml'
    See: https://docs.zephyrproject.org/latest/develop/test/twister.html#hardware-map
    """

    extra_flash_args: list[str] = field(default_factory=list)
    """Additional arguments to pass to west flash (e.g., ['--openocd', '/custom/path/openocd'])"""

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()

        # Create workspace base directory if it doesn't exist
        os.makedirs(self.workspace_base, exist_ok=True)

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_west.client.WestClient"

    def _get_workspace_path(
        self, manifest_url: str, manifest_rev: str, client_id: str | None, zephyr_path: str = "zephyr"
    ) -> tuple[str, str]:
        """Get workspace and zephyr paths from manifest parameters

        Generates a unique workspace identifier from the manifest parameters
        and returns the full paths to the workspace directory and zephyr source.

        Args:
            manifest_url: Git repository URL for the manifest
            manifest_rev: Git revision (tag, branch, commit hash)
            client_id: Optional client identifier to prevent conflicts
            zephyr_path: Relative path to Zephyr within the workspace (default: "zephyr")

        Returns:
            Tuple of (workspace_path, zephyr_base)

        Example:
            >>> ws_path, zephyr_base = self._get_workspace_path(
            ...     "https://github.com/zephyrproject-rtos/zephyr",
            ...     "v3.5.0",
            ...     "dev-alice",
            ...     "zephyr"
            ... )
            >>> # ws_path = "/home/exporter/jumpstarter-workspaces/zephyr_v3.5.0_dev-alice"
            >>> # zephyr_base = "/home/exporter/jumpstarter-workspaces/zephyr_v3.5.0_dev-alice/zephyr"
        """
        from .utils import generate_workspace_id

        workspace_id = generate_workspace_id(manifest_url, manifest_rev, client_id)
        workspace_path = os.path.join(self.workspace_base, workspace_id)
        zephyr_base = os.path.join(workspace_path, zephyr_path)
        return workspace_path, zephyr_base

    def _validate_workspace(self, workspace_path: str):
        """Validate that workspace has been initialized

        Args:
            workspace_path: Path to the workspace directory

        Raises:
            ValueError: If workspace doesn't exist or isn't initialized
        """
        west_config = os.path.join(workspace_path, ".west")
        if not os.path.exists(west_config):
            raise ValueError(
                f"Workspace not initialized at {workspace_path}. "
                "Run 'initialize_workspace' with the same manifest parameters first."
            )

        if self.sdk_path and not os.path.isdir(self.sdk_path):
            raise ValueError(
                f"Zephyr SDK directory does not exist: {self.sdk_path}\n"
                "Run 'install_sdk' to install the Zephyr SDK."
            )

    def _get_venv_path(self, workspace_path: str) -> str:
        """Get path to the Python virtual environment for a workspace

        Args:
            workspace_path: Path to the workspace directory

        Returns:
            Path to the venv directory
        """
        return os.path.join(workspace_path, ".venv")

    async def _ensure_venv(self, workspace_path: str) -> AsyncGenerator[str, None]:
        """Ensure Python virtual environment exists for workspace

        Creates a new venv if it doesn't exist, with pip installed and upgraded.
        This is idempotent - safe to call multiple times.

        Args:
            workspace_path: Path to the workspace directory

        Yields:
            Status messages during venv creation

        Note:
            After all messages are yielded, the venv bin path is available at
            `os.path.join(workspace_path, ".venv", "bin")`

        Example:
            >>> async for line in self._ensure_venv("/path/to/workspace"):
            ...     print(line)
        """
        import venv

        venv_path = self._get_venv_path(workspace_path)

        if not os.path.exists(venv_path):
            yield f"Creating Python virtual environment at {venv_path}"
            # Create venv with pip
            venv.create(venv_path, with_pip=True, symlinks=True)

            # Upgrade pip to latest version
            pip_path = os.path.join(venv_path, "bin", "pip")
            yield "Upgrading pip..."
            async for line in self._stream_cmd([pip_path, "install", "--upgrade", "pip"]):
                yield line

            # Install west into the venv so every subsequent `west ...` call (update,
            # twister, flash, sdk install) resolves to the venv's west — whose shebang
            # points to this venv's python. Without this, PATH falls back to a system
            # west whose sys.executable is some other venv, and twister ends up spawning
            # pytest under a Python that doesn't have the workspace's installed deps.
            yield "Installing west into venv..."
            async for line in self._stream_cmd([pip_path, "install", "west"]):
                yield line

    async def _stream_cmd(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> AsyncGenerator[str, None]:
        """Run a subprocess and yield merged stdout/stderr line by line.

        Each line is yielded to the caller as soon as it's available, which gives
        the client a live view of progress and — equally important for long runs
        like twister — keeps the gRPC stream active so HTTP/2 keepalive pings
        don't trip ``UNAVAILABLE: ping timeout``.

        Args:
            cmd: Command and arguments to execute
            cwd: Working directory for the command
            env: Environment variables (must be provided with venv activated)

        Raises:
            RuntimeError: If the process exits with non-zero status

        Yields:
            Output lines from the command
        """
        if env is None:
            env = os.environ.copy()

        self.logger.debug("Running command: %s", " ".join(cmd))
        print("Running command: %s", " ".join(cmd))

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdout is not None

        tail: deque[str] = deque(maxlen=50)
        async for raw in process.stdout:
            # west / git progress sometimes uses CR; strip both so log viewers
            # don't render control chars.
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            tail.append(line)
            yield line

        rc = await process.wait()
        if rc != 0:
            tail_text = "\n".join(tail)
            raise RuntimeError(
                f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed (rc={rc}):\n{tail_text}"
            )

    @export
    async def flash(
        self,
        src: str,
        manifest_url: str,
        manifest_rev: str,
        client_id: str | None = None,
        zephyr_path: str = "zephyr",
    ) -> AsyncGenerator[str, None]:
        """Flash firmware to the target board using a build directory archive

        Receives a tar archive of the Zephyr build directory, extracts it to a
        temporary directory, and runs ``west flash --no-rebuild --build-dir``.
        The build directory contains all the metadata west needs (board name,
        runner configuration, firmware binaries).

        The runner and other flash options are configured via the driver's
        extra_flash_args configuration parameter by the exporter administrator.

        Args:
            src: Streaming resource handle for the build directory tar archive
            manifest_url: Git repository URL for the manifest
            manifest_rev: Git revision (tag, branch, or commit hash)
            client_id: Optional client identifier to identify the workspace
            zephyr_path: Relative path to Zephyr within the workspace (default: "zephyr")

        Yields:
            Command output lines as they are produced.
        """
        # Get workspace paths
        workspace_path, zephyr_base = self._get_workspace_path(manifest_url, manifest_rev, client_id, zephyr_path)

        # Validate workspace exists
        self._validate_workspace(workspace_path)

        venv_bin = os.path.join(workspace_path, ".venv", "bin")

        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = os.path.join(tmpdir, "build.tar")
            build_dir = os.path.join(tmpdir, "build")
            os.makedirs(build_dir)

            async with await FileWriteStream.from_path(archive_path) as stream:
                async with self.resource(src) as res:
                    async for chunk in res:
                        await stream.send(chunk)

            with tarfile.open(archive_path) as tar:
                tar.extractall(build_dir)

            cmd = [self.west_path, "flash", "--no-rebuild", "--build-dir", build_dir]
            cmd.extend(self.extra_flash_args)

            env = self._build_env(workspace_path, venv_bin, zephyr_base)
            async for line in self._stream_cmd(cmd, cwd=zephyr_base, env=env):
                yield line

    @export
    async def initialize_workspace(
        self,
        manifest_url: str = "https://github.com/zephyrproject-rtos/zephyr",
        manifest_rev: str | None = None,
        manifest_file: str | None = None,
        client_id: str | None = None,
        force_recreate: bool = False,
        zephyr_path: str = "zephyr",
        post_init_command: str | None = None,
        url: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """Initialize a Zephyr workspace using west init or from a URL

        Sets up a new Zephyr workspace on the exporter. This command:
        1. Generates a unique workspace identifier from manifest parameters
        2. Optionally deletes existing workspace if force_recreate=True
        3. If url is provided:
           - Downloads the workspace archive from the URL
           - Extracts it to the workspace path
           - Ensures the venv exists
        4. If url is not provided:
           - Runs 'west init' if the workspace doesn't exist
           - Creates a Python virtual environment for the workspace
           - Checks out a specific version if manifest_rev is provided
           - Runs 'west update' to fetch all dependencies
        5. Optionally runs a user-defined shell command (post_init_command)

        Args:
            manifest_url: Git repository URL for the manifest (default: Zephyr main repo)
            manifest_rev: Git revision to checkout (tag, branch, or commit hash)
                         If None, uses "main"
            manifest_file: Manifest file to use (default: west.yml)
            client_id: Optional client identifier to isolate workspaces between users/CI
            force_recreate: If True, delete and recreate the workspace from scratch
            zephyr_path: Relative path to Zephyr within the workspace (default: "zephyr")
            post_init_command: Shell command to run after west update completes. Runs via
                              `bash -c` with cwd=workspace_path and the venv activated.
                              Example: "pip install -r external-module/openthread-tests.git/requirements.txt"
            url: URL to fetch a pre-built workspace archive (tar, tar.gz, tar.bz2, or tar.xz).
                When provided, the workspace is fetched from this URL instead of running west init/update.

        Yields:
            Command output lines as they are produced.

        Example:
            # Developer workflow (reuse workspace)
            >>> async for line in driver.initialize_workspace(
            ...     manifest_url="https://github.com/zephyrproject-rtos/zephyr",
            ...     manifest_rev="v3.5.0",
            ...     client_id="dev-alice"
            ... ):
            ...     print(line)

            # CI workflow (always fresh workspace)
            >>> async for line in driver.initialize_workspace(
            ...     manifest_url="https://github.com/zephyrproject-rtos/zephyr",
            ...     manifest_rev="v3.5.0",
            ...     client_id="ci",
            ...     force_recreate=True
            ... ):
            ...     print(line)
        """
        # Use "main" as default revision if not specified
        if manifest_rev is None:
            manifest_rev = "main"

        # Get workspace paths from manifest parameters
        workspace_path, zephyr_base = self._get_workspace_path(manifest_url, manifest_rev, client_id, zephyr_path)

        yield f"Workspace: {workspace_path}"

        # Force recreate: delete entire workspace directory
        if force_recreate and os.path.exists(workspace_path):
            yield f"Force recreate: removing existing workspace at {workspace_path}"
            shutil.rmtree(workspace_path)

        # Create workspace directory
        os.makedirs(workspace_path, exist_ok=True)

        # If URL is provided, fetch and extract the workspace from the URL
        if url:
            yield f"Fetching workspace from URL: {url}"

            with tempfile.NamedTemporaryFile(suffix=".tar", delete=True) as tmp:
                # Download the archive
                yield "Downloading workspace archive..."
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, urllib.request.urlretrieve, url, tmp.name)
                yield "Download complete"

                # Extract the archive to the workspace path
                yield f"Extracting workspace to {workspace_path}"
                with tarfile.open(tmp.name, "r:*") as tar:
                    tar.extractall(workspace_path)
                yield "Extraction complete"

            # Ensure the Python venv exists (it might be in the archive or need to be created)
            async for line in self._ensure_venv(workspace_path):
                yield line

            venv_bin = os.path.join(workspace_path, ".venv", "bin")
        else:
            # Standard west init workflow
            # Ensure the Python venv exists FIRST so every west invocation below uses
            # the venv's `west` (and therefore the venv's Python as sys.executable).
            async for line in self._ensure_venv(workspace_path):
                yield line

            venv_bin = os.path.join(workspace_path, ".venv", "bin")
            west_config = os.path.join(workspace_path, ".west")

            # Initialize west if not already done
            if os.path.exists(west_config):
                yield f"Workspace already initialized at {workspace_path}"
            else:
                cmd = [self.west_path, "init"]
                if manifest_file:
                    cmd.extend(["-mf", manifest_file])
                cmd.extend(["--mr", manifest_rev])
                cmd.extend(["-m", manifest_url])
                cmd.append(workspace_path)

                yield f"Initializing west workspace at {workspace_path}"
                env = self._build_env(workspace_path, venv_bin, zephyr_base)
                async for line in self._stream_cmd(cmd, cwd=workspace_path, env=env):
                    yield line

            # Checkout specific revision if workspace was already initialized
            if manifest_rev and os.path.exists(west_config) and not force_recreate:
                if os.path.exists(zephyr_base):
                    yield f"Checking out revision {manifest_rev}"
                    env = self._build_env(workspace_path, venv_bin, zephyr_base)
                    async for line in self._stream_cmd(
                        ["git", "checkout", manifest_rev],
                        cwd=zephyr_base,
                        env=env,
                    ):
                        yield line

            # Run west update to fetch all dependencies
            yield "Running west update..."
            async for line in self._update_workspace_impl(workspace_path, venv_bin, zephyr_base):
                yield line

        # Run user-defined post-init shell command if provided
        if post_init_command:
            yield f"Running post-init command: {post_init_command}"
            env = self._build_env(workspace_path, venv_bin, zephyr_base)
            async for line in self._stream_cmd(
                ["bash", "-c", post_init_command],
                cwd=workspace_path,
                env=env,
            ):
                yield line

        yield f"Workspace initialization complete: {workspace_path}"

    @export
    async def update_workspace(
        self,
        manifest_url: str = "https://github.com/zephyrproject-rtos/zephyr",
        manifest_rev: str | None = None,
        client_id: str | None = None,
        zephyr_path: str = "zephyr",
    ) -> AsyncGenerator[str, None]:
        """Update workspace dependencies using west update

        Fetches and updates all projects defined in the west manifest.
        This is equivalent to running 'west update' in the workspace.

        Args:
            manifest_url: Git repository URL for the manifest
            manifest_rev: Git revision (tag, branch, or commit hash). If None, uses "main"
            client_id: Optional client identifier to identify the workspace
            zephyr_path: Relative path to Zephyr within the workspace (default: "zephyr")

        Yields:
            Command output lines as they are produced.
        """
        # Use "main" as default revision if not specified
        if manifest_rev is None:
            manifest_rev = "main"

        # Get workspace paths
        workspace_path, zephyr_base = self._get_workspace_path(manifest_url, manifest_rev, client_id, zephyr_path)

        # Validate workspace exists
        self._validate_workspace(workspace_path)

        venv_bin = os.path.join(workspace_path, ".venv", "bin")

        # Run update
        async for line in self._update_workspace_impl(workspace_path, venv_bin, zephyr_base):
            yield line

    async def _update_workspace_impl(
        self, workspace_path: str, venv_bin: str, zephyr_base: str
    ) -> AsyncGenerator[str, None]:
        """Shared implementation for updating workspace dependencies

        Args:
            workspace_path: Path to the workspace directory
            venv_bin: Path to the venv bin directory
            zephyr_base: Path to the Zephyr directory
        """
        west_config = os.path.join(workspace_path, ".west")
        if not os.path.exists(west_config):
            raise ValueError(
                f"Workspace not initialized at {workspace_path}. "
                "Run 'initialize_workspace' first."
            )

        # Build environment with venv
        env = self._build_env(workspace_path, venv_bin, zephyr_base)

        yield "Updating workspace dependencies"
        async for line in self._stream_cmd(
            [self.west_path, "update"],
            cwd=workspace_path,
            env=env,
        ):
            yield line

        # Install Python requirements using venv pip. Fail loudly if the expected
        # requirements file is missing — that almost always means zephyr_path was
        # wrong (e.g., Zephyr lives under "third-party/zephyr" but caller used the
        # default "zephyr"). Silently skipping leaves the venv missing jsonschema /
        # pyelftools etc. and the failure only surfaces much later inside twister.
        req_file = os.path.join(zephyr_base, "scripts", "requirements.txt")
        if not os.path.isfile(req_file):
            raise ValueError(
                f"Zephyr requirements file not found at {req_file}. "
                f"Check that zephyr_path correctly points to the Zephyr source "
                f"directory inside the workspace (e.g., pass --zephyr-path "
                f"third-party/zephyr if Zephyr is fetched there)."
            )

        yield f"Installing Python requirements from {req_file}"

        # Check for uv in venv first, then use pip
        uv_path = os.path.join(venv_bin, "uv")
        pip_path = os.path.join(venv_bin, "pip")

        if os.path.exists(uv_path):
            pip_cmd = [uv_path, "pip", "install", "-r", req_file]
        else:
            pip_cmd = [pip_path, "install", "-r", req_file]

        async for line in self._stream_cmd(pip_cmd, env=env):
            yield line

    @export
    async def install_sdk(
        self,
        toolchains: list[str] | None = None,
        manifest_url: str = "https://github.com/zephyrproject-rtos/zephyr",
        manifest_rev: str | None = None,
        client_id: str | None = None,
        zephyr_path: str = "zephyr",
    ) -> AsyncGenerator[str, None]:
        """Install Zephyr SDK using west sdk install

        Downloads and installs the Zephyr SDK. The SDK will be installed in the
        location specified by the sdk_path configuration parameter.

        Args:
            toolchains: List of specific toolchains to install (e.g., ['arm', 'riscv'])
                       If None, installs all available toolchains
            manifest_url: Git repository URL for the manifest (default: Zephyr main repo)
            manifest_rev: Git revision (tag, branch, or commit hash). If None, uses "main"
            client_id: Optional client identifier to identify the workspace
            zephyr_path: Relative path to Zephyr within the workspace (default: "zephyr")

        Yields:
            Command output lines as they are produced.
        """
        if not self.sdk_path:
            raise ValueError(
                "sdk_path must be configured to use install_sdk. "
                "Set sdk_path in the driver configuration."
            )

        # Use "main" as default revision if not specified
        if manifest_rev is None:
            manifest_rev = "main"

        # Get workspace paths
        workspace_path, zephyr_base = self._get_workspace_path(manifest_url, manifest_rev, client_id, zephyr_path)

        # Validate workspace exists
        self._validate_workspace(workspace_path)

        venv_bin = os.path.join(workspace_path, ".venv", "bin")

        os.makedirs(self.sdk_path, exist_ok=True)

        cmd = [self.west_path, "sdk", "install"]
        if toolchains:
            cmd.extend(["-t", ",".join(toolchains)])

        env = self._build_env(workspace_path, venv_bin, zephyr_base)
        env["ZEPHYR_SDK_INSTALL_DIR"] = self.sdk_path

        yield f"Installing Zephyr SDK to {self.sdk_path}"
        async for line in self._stream_cmd(cmd, cwd=workspace_path, env=env):
            yield line


    def get_compression_from_tarfile(self, filepath: str) -> str:
        for compression in ('gz', 'bz2', 'xz'):
            try:
                mode = f"r:{compression}"
                with tarfile.open(filepath, mode) as tar:
                    return compression
            except tarfile.ReadError:
                continue
        return None

    @export
    async def twister(
        self,
        src: str,
        test_roots: list[str],
        manifest_url: str,
        manifest_rev: str,
        client_id: str | None = None,
        zephyr_path: str = "zephyr",
        extra_twister_args: list[str] | None = None,
    ) -> AsyncGenerator[str, None]:
        """Run twister in test-only mode using a pre-built twister-out archive

        Receives a tar archive produced by the build server (containing the
        ``twister-out`` directory), extracts it into the workspace, then runs
        ``west twister --test-only --device-testing``. The updated
        ``twister-out`` directory is compressed and kept on the exporter until
        ``twister_fetch_results`` is called.

        The archive can be in any format supported by tarfile (tar, tar.gz, tar.bz2, tar.xz).
        The output will use the same compression format as the input.

        Args:
            src: Streaming resource handle for the twister-out tar archive
            test_roots: List of test root paths passed to twister via ``-T``
            manifest_url: Git repository URL for the manifest
            manifest_rev: Git revision (tag, branch, or commit hash)
            client_id: Optional client identifier to identify the workspace
            zephyr_path: Relative path to Zephyr within the workspace (default: "zephyr")
            extra_twister_args: Additional arguments to pass to twister (e.g., ['--pytest-args=-v', '--log-level=DEBUG'])

        Yields:
            Command output lines as they are produced.
        """
        if not self.hardware_map:
            raise ValueError(
                "hardware_map must be configured to use twister. "
                "Set hardware_map in the driver configuration."
            )

        # Get workspace paths
        workspace_path, zephyr_base = self._get_workspace_path(manifest_url, manifest_rev, client_id, zephyr_path)

        # Validate workspace exists
        self._validate_workspace(workspace_path)

        venv_bin = os.path.join(workspace_path, ".venv", "bin")

        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = os.path.join(tmpdir, "twister-out.tar")

            async with await FileWriteStream.from_path(archive_path) as stream:
                async with self.resource(src) as res:
                    async for chunk in res:
                        await stream.send(chunk)

            # Detect the compression format from the input archive
            compression = self.get_compression_from_tarfile(archive_path)

            with tarfile.open(archive_path, "r:*") as tar:
                tar.extractall(workspace_path)

        twister_out = os.path.join(workspace_path, "twister-out")

        cmd = [
            self.west_path,
            "twister",
            "--test-only",
            "--device-testing",
            "--hardware-map", self.hardware_map,
        ]
        for root in test_roots:
            cmd.extend(["-T", root])
        if extra_twister_args:
            cmd.extend(extra_twister_args)

        yield f"Running twister with hardware map {self.hardware_map}"

        # Capture any test failures but continue to compress results
        test_error = None
        try:
            env = self._build_env(workspace_path, venv_bin, zephyr_base)
            async for line in self._stream_cmd(cmd, cwd=workspace_path, env=env):
                yield line
        except RuntimeError as e:
            test_error = e
            yield f"Twister test failed: {e}"

        # Use the same compression format as the input
        compression_suffix = f".{compression}" if compression else ""
        result_archive = os.path.join(
            workspace_path, f"twister-out-result.tar{compression_suffix}"
        )
        write_mode = f"w:{compression}" if compression else "w"

        yield f"Compressing twister results using {compression or 'no compression'}"
        # tarfile.add walks the tree synchronously; for typical twister-out sizes
        # (tens of MB) this is fast enough that we don't bother offloading it.
        with tarfile.open(result_archive, write_mode) as tar:
            tar.add(twister_out, arcname="twister-out")

        if test_error:
            yield "Results compressed and ready for retrieval despite test failure"

    @export
    async def twister_fetch_results(
        self,
        dst: str,
        manifest_url: str,
        manifest_rev: str,
        client_id: str | None = None,
        zephyr_path: str = "zephyr",
    ) -> None:
        """Stream the twister result archive back to the client

        Streams the ``twister-out-result.tar*`` archive produced by the last
        ``twister`` call to the client and removes it from the exporter.
        The archive format matches the input format used in the twister call.

        Args:
            dst: Streaming resource handle to write the result archive to
            manifest_url: Git repository URL for the manifest
            manifest_rev: Git revision (tag, branch, or commit hash)
            client_id: Optional client identifier to identify the workspace
            zephyr_path: Relative path to Zephyr within the workspace (default: "zephyr")
        """
        import glob

        # Get workspace paths
        workspace_path, _ = self._get_workspace_path(manifest_url, manifest_rev, client_id, zephyr_path)

        # Find the result archive with any compression format
        pattern = os.path.join(workspace_path, "twister-out-result.tar*")
        matches = glob.glob(pattern)

        if not matches:
            raise FileNotFoundError(
                "No twister results found on exporter. Run twister first."
            )

        if len(matches) > 1:
            raise RuntimeError(
                f"Multiple twister result archives found: {matches}. "
                "Clean up the workspace before running twister again."
            )

        result_archive = matches[0]
        try:
            async with await FileReadStream.from_path(result_archive) as file_stream:
                async with self.resource(dst) as res:
                    async for chunk in file_stream:
                        await res.send(chunk)
        finally:
            os.unlink(result_archive)

    def _build_env(self, workspace_path: str, venv_bin: str, zephyr_base: str) -> dict[str, str]:
        """Build environment variables for west commands with venv activated

        Args:
            workspace_path: Path to the workspace directory
            venv_bin: Path to the venv bin directory
            zephyr_base: Path to the Zephyr directory

        Returns:
            Dictionary of environment variables with venv activated
        """
        env = os.environ.copy()

        # Set ZEPHYR_BASE to the provided zephyr path
        env["ZEPHYR_BASE"] = zephyr_base

        # Activate venv by prepending to PATH and setting VIRTUAL_ENV
        env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
        env["VIRTUAL_ENV"] = os.path.dirname(venv_bin)

        # Set SDK path if configured
        if self.sdk_path:
            env["ZEPHYR_SDK_INSTALL_DIR"] = self.sdk_path

        return env
