# Copyright (c) 2026 BayLibre
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field

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
    - (Future) Run twister tests on remote hardware
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

    @export
    async def flash(self, src: str) -> str:
        """Flash firmware to the target board using a build directory archive

        Receives a tar archive of the Zephyr build directory, extracts it to a
        temporary directory, and runs ``west flash --no-rebuild --build-dir``.
        The build directory contains all the metadata west needs (board name,
        runner configuration, firmware binaries).

        The runner and other flash options are configured via the driver's
        extra_flash_args configuration parameter by the exporter administrator.

        Args:
            src: Streaming resource handle for the build directory tar archive

        Returns:
            Command output
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

            cmd = ["flash", "--no-rebuild", "--build-dir", build_dir]
            cmd.extend(self.extra_flash_args)

            return self._run_cmd(cmd)

    @export
    def initialize_workspace(
        self,
        manifest_url: str = "https://github.com/zephyrproject-rtos/zephyr",
        manifest_rev: str | None = None,
        manifest_file: str | None = None,
    ) -> str:
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

        Returns:
            Command output

        Example:
            # Initialize with latest Zephyr
            initialize_workspace()

            # Initialize with specific version
            initialize_workspace(manifest_rev="v3.5.0")

            # Initialize with custom manifest
            initialize_workspace(
                manifest_url="https://github.com/myorg/zephyr-manifest",
                manifest_rev="main"
            )
        """
        west_config = os.path.join(self.workspace_path, ".west")

        # Check if workspace is already initialized
        if os.path.exists(west_config):
            self.logger.info("Workspace already initialized at %s", self.workspace_path)
            output = "Workspace already initialized\n"
        else:
            # Run west init
            cmd = [self.west_path, "init"]

            if manifest_file:
                cmd.extend(["-m", manifest_file])

            if manifest_rev:
                cmd.extend(["--mr", manifest_rev])

            cmd.append(self.workspace_path)

            self.logger.info("Initializing workspace at %s", self.workspace_path)
            self.logger.debug("Running command: %s", " ".join(cmd))

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=os.environ.copy(),
            )

            if result.returncode != 0:
                self.logger.error("Error initializing workspace: %s", result.stderr)
                raise RuntimeError(f"West init failed: {result.stderr}")

            output = result.stdout

        # If manifest_rev was specified and workspace was already initialized,
        # checkout the requested revision
        if manifest_rev and os.path.exists(west_config):
            manifest_dir = os.path.join(self.workspace_path, "zephyr")
            if os.path.exists(manifest_dir):
                self.logger.info("Checking out revision %s", manifest_rev)
                git_cmd = ["git", "checkout", manifest_rev]
                result = subprocess.run(
                    git_cmd,
                    capture_output=True,
                    text=True,
                    cwd=manifest_dir,
                    env=os.environ.copy(),
                )

                if result.returncode != 0:
                    self.logger.error("Error checking out revision: %s", result.stderr)
                    raise RuntimeError(f"Git checkout failed: {result.stderr}")

                output += result.stdout

        # Run west update to fetch dependencies
        self.logger.info("Updating workspace dependencies")
        update_output = self.update_workspace()
        output += "\n" + update_output

        return output

    @export
    def update_workspace(self) -> str:
        """Update workspace dependencies using west update

        Fetches and updates all projects defined in the west manifest.
        This is equivalent to running 'west update' in the workspace.

        Returns:
            Command output

        Example:
            update_workspace()
        """
        west_config = os.path.join(self.workspace_path, ".west")
        if not os.path.exists(west_config):
            raise ValueError(f"Workspace not initialized at {self.workspace_path}. Run 'initialize_workspace' first.")

        cmd = [self.west_path, "update"]
        self.logger.info("Updating workspace dependencies")
        self.logger.debug("Running command: %s", " ".join(cmd))

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=self.workspace_path,
            env=os.environ.copy(),
        )

        if result.returncode != 0:
            self.logger.error("Error updating workspace: %s", result.stderr)
            raise RuntimeError(f"West update failed: {result.stderr}")

        output = result.stdout

        req_file = os.path.join(self.zephyr_base, "scripts", "requirements.txt")
        if os.path.isfile(req_file):
            self.logger.info("Installing Python requirements from %s", req_file)
            if shutil.which("uv"):
                pip_cmd = ["uv", "pip", "install", "-r", req_file]
            else:
                pip_cmd = [sys.executable, "-m", "pip", "install", "-r", req_file]
            pip_result = subprocess.run(pip_cmd, capture_output=True, text=True)
            if pip_result.returncode != 0:
                self.logger.error("Error installing requirements: %s", pip_result.stderr)
                raise RuntimeError(f"pip install failed: {pip_result.stderr}")
            output += pip_result.stdout

        return output

    @export
    def install_sdk(self, toolchains: list[str] | None = None) -> str:
        """Install Zephyr SDK using west sdk install

        Downloads and installs the Zephyr SDK. The SDK will be installed in the
        location specified by the sdk_path configuration parameter.

        Args:
            toolchains: List of specific toolchains to install (e.g., ['arm', 'riscv'])
                       If None, installs all available toolchains

        Returns:
            Command output

        Example:
            # Install all toolchains
            install_sdk()

            # Install specific toolchains
            install_sdk(toolchains=["arm", "riscv"])
        """
        if not self.sdk_path:
            raise ValueError(
                "sdk_path must be configured to use install_sdk. Set sdk_path in the driver configuration."
            )

        # Create SDK directory if it doesn't exist
        os.makedirs(self.sdk_path, exist_ok=True)

        cmd = [self.west_path, "sdk", "install"]

        if toolchains:
            cmd.extend(["-t", ",".join(toolchains)])

        self.logger.info("Installing Zephyr SDK to %s", self.sdk_path)
        self.logger.debug("Running command: %s", " ".join(cmd))

        env = os.environ.copy()
        env["ZEPHYR_SDK_INSTALL_DIR"] = self.sdk_path

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=self.workspace_path,
            env=env,
        )

        if result.returncode != 0:
            self.logger.error("Error installing SDK: %s", result.stderr)
            raise RuntimeError(f"West SDK install failed: {result.stderr}")

        return result.stdout

    @export
    async def twister(self, src: str, test_roots: list[str]) -> str:
        """Run twister in test-only mode using a pre-built twister-out archive

        Receives a tar.gz archive produced by the build server (containing the
        ``twister-out`` directory), extracts it into the workspace, then runs
        ``west twister --test-only --device-testing``. The updated
        ``twister-out`` directory is compressed and kept on the exporter until
        ``twister_fetch_results`` is called.

        Args:
            src: Streaming resource handle for the twister-out tar.gz archive
            test_roots: List of test root paths passed to twister via ``-T``

        Returns:
            Command output
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
            archive_path = os.path.join(tmpdir, "twister-out.tar.gz")

            async with await FileWriteStream.from_path(archive_path) as stream:
                async with self.resource(src) as res:
                    async for chunk in res:
                        await stream.send(chunk)

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

        env = self._build_env()

        self.logger.info("Running twister with hardware map %s", self.hardware_map)
        self.logger.debug("Running command: %s", " ".join(cmd))

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=self.workspace_path,
        )

        if result.returncode != 0:
            self.logger.error("Twister failed: %s", result.stderr)
            raise RuntimeError(f"Twister failed: {result.stderr}")

        self.logger.info("Compressing twister results")
        result_archive = os.path.join(self.workspace_path, "twister-out-result.tar.gz")
        with tarfile.open(result_archive, "w:gz") as tar:
            tar.add(twister_out, arcname="twister-out")

        return result.stdout

    @export
    async def twister_fetch_results(self, dst: str) -> None:
        """Stream the twister result archive back to the client

        Streams the ``twister-out-result.tar.gz`` archive produced by the last
        ``twister`` call to the client and removes it from the exporter.

        Args:
            dst: Streaming resource handle to write the result archive to
        """
        result_archive = os.path.join(self.workspace_path, "twister-out-result.tar.gz")
        if not os.path.exists(result_archive):
            raise FileNotFoundError(
                "No twister results found on exporter. Run twister first."
            )
        try:
            async with await FileReadStream.from_path(result_archive) as file_stream:
                async with self.resource(dst) as res:
                    async for chunk in file_stream:
                        await res.send(chunk)
        finally:
            os.unlink(result_archive)

    def _run_cmd(self, cmd: list[str]) -> str:
        """Run a west command with proper environment setup

        Args:
            cmd: Command arguments (without 'west' prefix)

        Returns:
            Command output (stdout)
        """
        full_cmd = [self.west_path, *cmd]
        env = self._build_env()

        self.logger.debug("Running command: %s", " ".join(full_cmd))

        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=self.zephyr_base,
        )

        if result.returncode != 0:
            self.logger.error("Error running %s: %s", full_cmd, result.stderr)
            raise RuntimeError(f"West command failed: {result.stderr}")

        self.logger.debug("Command output: %s", result.stdout)
        return result.stdout

    def _build_env(self) -> dict[str, str]:
        """Build environment variables for west commands

        Returns:
            Dictionary of environment variables
        """
        env = os.environ.copy()
        env["ZEPHYR_BASE"] = self.zephyr_base
        env["ZEPHYR_SDK_INSTALL_DIR"] = self.sdk_path
        return env
