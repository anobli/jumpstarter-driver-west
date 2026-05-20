# Copyright (c) 2026 BayLibre
# SPDX-License-Identifier: Apache-2.0

import asyncio
import asyncio.subprocess
import os
import shutil
import sys
import tarfile
import tempfile
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

    The driver manages the Zephyr workspace on the exporter side, allowing you to:
    - Initialize and update Zephyr workspaces
    - Install the Zephyr SDK
    - Flash firmware to remote boards
    - Run twister tests on remote hardware
    """

    workspace_path: str
    """Path to the West workspace on the exporter (required).

    This is where west will manage the Zephyr projects. You can have multiple workspaces
    to avoid conflicts between different versions or configurations.
    Example: '/home/exporter/zephyr-workspace'
    """

    zephyr_base: str | None = None
    """Path to the Zephyr source tree on the exporter (optional).

    If not specified, defaults to {workspace_path}/zephyr.
    Only needs to be set if you have a custom Zephyr location.
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

        # Auto-detect zephyr_base if not specified
        if self.zephyr_base is None:
            self.zephyr_base = os.path.join(self.workspace_path, "zephyr")

        # Create workspace directory if it doesn't exist
        os.makedirs(self.workspace_path, exist_ok=True)

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_west.client.WestClient"

    def _validate_paths(self):
        """Validate that required paths exist for flash operations"""
        if not os.path.isdir(self.zephyr_base):
            raise ValueError(
                f"Zephyr base directory does not exist: {self.zephyr_base}\n"
                "Run 'initialize_workspace' to set up the workspace first."
            )

        if self.sdk_path and not os.path.isdir(self.sdk_path):
            raise ValueError(
                f"Zephyr SDK directory does not exist: {self.sdk_path}\nRun 'install_sdk' to install the Zephyr SDK."
            )

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

        Raises RuntimeError if the process exits non-zero; the message includes
        the tail of the output for context.
        """
        if env is None:
            env = self._build_env()

        self.logger.debug("Running command: %s", " ".join(cmd))

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
    async def flash(self, src: str) -> AsyncGenerator[str, None]:
        """Flash firmware to the target board using a build directory archive

        Receives a tar archive of the Zephyr build directory, extracts it to a
        temporary directory, and runs ``west flash --no-rebuild --build-dir``.
        The build directory contains all the metadata west needs (board name,
        runner configuration, firmware binaries).

        The runner and other flash options are configured via the driver's
        extra_flash_args configuration parameter by the exporter administrator.

        Args:
            src: Streaming resource handle for the build directory tar archive

        Yields:
            Command output lines as they are produced.
        """
        self._validate_paths()

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

            async for line in self._stream_cmd(cmd, cwd=self.zephyr_base):
                yield line

    @export
    async def initialize_workspace(
        self,
        manifest_url: str = "https://github.com/zephyrproject-rtos/zephyr",
        manifest_rev: str | None = None,
        manifest_file: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """Initialize a Zephyr workspace using west init

        Sets up a new Zephyr workspace on the exporter. This command:
        1. Runs 'west init' if the workspace doesn't exist
        2. Checks out a specific version if manifest_rev is provided
        3. Runs 'west update' to fetch all dependencies

        Args:
            manifest_url: Git repository URL for the manifest (default: Zephyr main repo)
            manifest_rev: Git revision to checkout (tag, branch, or commit hash)
                         If None, uses the default branch
            manifest_file: Manifest file to use (default: west.yml)

        Yields:
            Command output lines as they are produced.
        """
        west_config = os.path.join(self.workspace_path, ".west")

        if os.path.exists(west_config):
            yield f"Workspace already initialized at {self.workspace_path}"
        else:
            cmd = [self.west_path, "init"]
            if manifest_file:
                cmd.extend(["-m", manifest_file])
            if manifest_rev:
                cmd.extend(["--mr", manifest_rev])
            cmd.append(self.workspace_path)

            yield f"Initializing workspace at {self.workspace_path}"
            async for line in self._stream_cmd(cmd):
                yield line

        if manifest_rev and os.path.exists(west_config):
            manifest_dir = os.path.join(self.workspace_path, "zephyr")
            if os.path.exists(manifest_dir):
                yield f"Checking out revision {manifest_rev}"
                async for line in self._stream_cmd(
                    ["git", "checkout", manifest_rev],
                    cwd=manifest_dir,
                ):
                    yield line

        async for line in self._update_workspace():
            yield line

    @export
    async def update_workspace(self) -> AsyncGenerator[str, None]:
        """Update workspace dependencies using west update

        Fetches and updates all projects defined in the west manifest.
        This is equivalent to running 'west update' in the workspace.

        Yields:
            Command output lines as they are produced.
        """
        async for line in self._update_workspace():
            yield line

    async def _update_workspace(self) -> AsyncGenerator[str, None]:
        """Shared body for update_workspace / initialize_workspace post-init."""
        west_config = os.path.join(self.workspace_path, ".west")
        if not os.path.exists(west_config):
            raise ValueError(
                f"Workspace not initialized at {self.workspace_path}. "
                "Run 'initialize_workspace' first."
            )

        yield "Updating workspace dependencies"
        async for line in self._stream_cmd(
            [self.west_path, "update"],
            cwd=self.workspace_path,
        ):
            yield line

        req_file = os.path.join(self.zephyr_base, "scripts", "requirements.txt")
        if os.path.isfile(req_file):
            yield f"Installing Python requirements from {req_file}"
            if shutil.which("uv"):
                pip_cmd = ["uv", "pip", "install", "-r", req_file]
            else:
                pip_cmd = [sys.executable, "-m", "pip", "install", "-r", req_file]
            async for line in self._stream_cmd(pip_cmd):
                yield line

    @export
    async def install_sdk(
        self, toolchains: list[str] | None = None
    ) -> AsyncGenerator[str, None]:
        """Install Zephyr SDK using west sdk install

        Downloads and installs the Zephyr SDK. The SDK will be installed in the
        location specified by the sdk_path configuration parameter.

        Args:
            toolchains: List of specific toolchains to install (e.g., ['arm', 'riscv'])
                       If None, installs all available toolchains

        Yields:
            Command output lines as they are produced.
        """
        if not self.sdk_path:
            raise ValueError(
                "sdk_path must be configured to use install_sdk. "
                "Set sdk_path in the driver configuration."
            )

        os.makedirs(self.sdk_path, exist_ok=True)

        cmd = [self.west_path, "sdk", "install"]
        if toolchains:
            cmd.extend(["-t", ",".join(toolchains)])

        env = self._build_env()
        env["ZEPHYR_SDK_INSTALL_DIR"] = self.sdk_path

        yield f"Installing Zephyr SDK to {self.sdk_path}"
        async for line in self._stream_cmd(cmd, cwd=self.workspace_path, env=env):
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
        self, src: str, test_roots: list[str]
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

        Yields:
            Command output lines as they are produced.
        """
        if not self.hardware_map:
            raise ValueError(
                "hardware_map must be configured to use twister. "
                "Set hardware_map in the driver configuration."
            )

        west_config = os.path.join(self.workspace_path, ".west")
        if not os.path.exists(west_config):
            raise ValueError(
                f"Workspace not initialized at {self.workspace_path}. "
                "Run 'initialize_workspace' first."
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = os.path.join(tmpdir, "twister-out.tar")

            async with await FileWriteStream.from_path(archive_path) as stream:
                async with self.resource(src) as res:
                    async for chunk in res:
                        await stream.send(chunk)

            # Detect the compression format from the input archive
            compression = self.get_compression_from_tarfile(archive_path)

            with tarfile.open(archive_path, "r:*") as tar:
                tar.extractall(self.workspace_path)

        twister_out = os.path.join(self.workspace_path, "twister-out")

        cmd = [
            self.west_path,
            "twister",
            "--test-only",
            "--device-testing",
            "--hardware-map", self.hardware_map,
        ]
        for root in test_roots:
            cmd.extend(["-T", root])

        yield f"Running twister with hardware map {self.hardware_map}"

        # Capture any test failures but continue to compress results
        test_error = None
        try:
            async for line in self._stream_cmd(cmd, cwd=self.workspace_path):
                yield line
        except RuntimeError as e:
            test_error = e
            yield f"Twister test failed: {e}"

        # Use the same compression format as the input
        compression_suffix = f".{compression}" if compression else ""
        result_archive = os.path.join(
            self.workspace_path, f"twister-out-result.tar{compression_suffix}"
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
    async def twister_fetch_results(self, dst: str) -> None:
        """Stream the twister result archive back to the client

        Streams the ``twister-out-result.tar*`` archive produced by the last
        ``twister`` call to the client and removes it from the exporter.
        The archive format matches the input format used in the twister call.

        Args:
            dst: Streaming resource handle to write the result archive to
        """
        import glob

        # Find the result archive with any compression format
        pattern = os.path.join(self.workspace_path, "twister-out-result.tar*")
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

    def _build_env(self) -> dict[str, str]:
        """Build environment variables for west commands

        Returns:
            Dictionary of environment variables
        """
        env = os.environ.copy()
        env["ZEPHYR_BASE"] = self.zephyr_base
        # Only set ZEPHYR_SDK_INSTALL_DIR if configured — otherwise
        # subprocess.Popen / asyncio.create_subprocess_exec choke on a None value.
        if self.sdk_path:
            env["ZEPHYR_SDK_INSTALL_DIR"] = self.sdk_path
        return env
