# Copyright (c) 2026 BayLibre
# SPDX-License-Identifier: Apache-2.0

import os
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import click
import yaml
from jumpstarter_driver_opendal.adapter import OpendalAdapter
from opendal import Operator

from jumpstarter.client import DriverClient
from jumpstarter.client.decorators import driver_click_group


@dataclass(kw_only=True)
class WestClient(DriverClient):
    """
    Client interface for remote Zephyr development using west.

    This client enables using standard Zephyr tools from your local machine to interact
    with hardware on a remote exporter. It provides methods to:
    - Set up and manage Zephyr workspaces on the exporter
    - Install the Zephyr SDK remotely
    - Flash Zephyr firmware to remote targets
    - Run twister tests on remote hardware

    All long-running operations are exposed as iterators of output lines so the
    caller can render them live. The CLI commands print each line as it arrives;
    Python callers can do the same or collect them into a string.
    """

    def _load_config(self) -> dict:
        """Load client configuration file

        Searches for configuration file in the following order:
        1. JUMPSTARTER_WEST_CONFIG environment variable
        2. .jumpstarter/west-config.yaml (current directory)
        3. ~/.jumpstarter/west-config.yaml (user home)
        4. Returns empty dict if no config found

        Returns:
            Dictionary containing configuration, or empty dict
        """
        import os
        import yaml

        paths = []

        # Check environment variable
        if "JUMPSTARTER_WEST_CONFIG" in os.environ:
            paths.append(os.environ["JUMPSTARTER_WEST_CONFIG"])

        # Check current directory and home directory
        paths.extend([
            ".jumpstarter/west-config.yaml",
            os.path.expanduser("~/.jumpstarter/west-config.yaml"),
        ])

        for path in paths:
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        config = yaml.safe_load(f)
                        return config if config else {}
                except Exception:
                    # Ignore errors reading config file
                    pass

        return {}

    def _get_zephyr_path(self, zephyr_path: str | None = None) -> str:
        """Get zephyr path from parameter or config file

        Args:
            zephyr_path: Explicit zephyr path (from CLI option)

        Returns:
            Zephyr path to use (defaults to "zephyr" if not configured)
        """
        if zephyr_path is not None:
            return zephyr_path

        config = self._load_config()
        return config.get("zephyr_path", "zephyr")

    def _get_extra_twister_args(self, extra_twister_args: list[str] | None = None) -> list[str]:
        """Get extra twister args from parameter or config file

        Args:
            extra_twister_args: Explicit extra twister args (from CLI option)

        Returns:
            List of extra twister arguments (defaults to empty list if not configured)
        """
        if extra_twister_args is not None:
            return extra_twister_args

        config = self._load_config()
        return config.get("extra_twister_args", [])

    def _get_post_init_command(self, post_init_command: str | None = None) -> str | None:
        """Get post-init shell command from parameter or config file

        Args:
            post_init_command: Explicit post-init command (from CLI option)

        Returns:
            Shell command to run after workspace init, or None if not configured
        """
        if post_init_command is not None:
            return post_init_command

        config = self._load_config()
        return config.get("post_init_command") or None

    def initialize_workspace(
        self,
        manifest_url: str | None = None,
        manifest_rev: str | None = None,
        manifest_file: str | None = None,
        client_id: str | None = None,
        force_recreate: bool = False,
        zephyr_path: str | None = None,
        post_init_command: str | None = None,
    ) -> Iterator[str]:
        """Initialize a Zephyr workspace on the exporter

        Auto-detects manifest parameters from the current directory's west workspace
        if not explicitly provided.

        Args:
            manifest_url: Git repository URL for the manifest (auto-detected if None)
            manifest_rev: Git revision to checkout (auto-detected if None)
            manifest_file: Manifest file to use (default: west.yml)
            client_id: Client identifier to isolate workspaces (from config if None)
            force_recreate: If True, delete and recreate the workspace from scratch
            zephyr_path: Relative path to Zephyr within workspace (from config if None, default: "zephyr")
            post_init_command: Shell command to run after init completes (from config if None).
                              Runs via `bash -c` with cwd=workspace_path and the venv activated.

        Yields:
            Command output lines, in real time.
        """
        from .detection import detect_west_workspace

        # Auto-detect from current directory if not provided
        if manifest_url is None or manifest_rev is None:
            detected = detect_west_workspace(".")
            if detected:
                manifest_url = manifest_url or detected["manifest_url"]
                manifest_rev = manifest_rev or detected["manifest_rev"]
                yield f"Auto-detected from current directory: {manifest_url} @ {manifest_rev}"

        # Get client_id from config if not provided
        if client_id is None:
            config = self._load_config()
            client_id = config.get("client_id")

        # Get zephyr_path from config if not provided
        zephyr_path = self._get_zephyr_path(zephyr_path)

        # Get post_init_command from config if not provided
        post_init_command = self._get_post_init_command(post_init_command)

        # Use default manifest_url if still None
        if manifest_url is None:
            manifest_url = "https://github.com/zephyrproject-rtos/zephyr"

        yield from self.streamingcall(
            "initialize_workspace",
            manifest_url,
            manifest_rev,
            manifest_file,
            client_id,
            force_recreate,
            zephyr_path,
            post_init_command,
        )

    def update_workspace(
        self,
        manifest_url: str | None = None,
        manifest_rev: str | None = None,
        client_id: str | None = None,
        zephyr_path: str | None = None,
    ) -> Iterator[str]:
        """Update workspace dependencies on the exporter

        Auto-detects manifest parameters from the current directory's west workspace
        if not explicitly provided.

        Args:
            manifest_url: Git repository URL for the manifest (auto-detected if None)
            manifest_rev: Git revision (auto-detected if None)
            client_id: Client identifier to identify the workspace (from config if None)
            zephyr_path: Relative path to Zephyr within workspace (from config if None, default: "zephyr")

        Yields:
            Command output lines, in real time.
        """
        from .detection import detect_west_workspace

        # Auto-detect from current directory if not provided
        if manifest_url is None or manifest_rev is None:
            detected = detect_west_workspace(".")
            if detected:
                manifest_url = manifest_url or detected["manifest_url"]
                manifest_rev = manifest_rev or detected["manifest_rev"]
                yield f"Auto-detected from current directory: {manifest_url} @ {manifest_rev}"

        # Get client_id from config if not provided
        if client_id is None:
            config = self._load_config()
            client_id = config.get("client_id")

        # Get zephyr_path from config if not provided
        zephyr_path = self._get_zephyr_path(zephyr_path)

        # Use default manifest_url if still None
        if manifest_url is None:
            manifest_url = "https://github.com/zephyrproject-rtos/zephyr"

        yield from self.streamingcall("update_workspace", manifest_url, manifest_rev, client_id, zephyr_path)

    def install_sdk(
        self,
        toolchains: list[str] | None = None,
        manifest_url: str | None = None,
        manifest_rev: str | None = None,
        client_id: str | None = None,
        zephyr_path: str | None = None,
    ) -> Iterator[str]:
        """Install Zephyr SDK on the exporter

        Auto-detects manifest parameters from the current directory's west workspace
        if not explicitly provided.

        Args:
            toolchains: List of specific toolchains to install
            manifest_url: Git repository URL for the manifest (auto-detected if None)
            manifest_rev: Git revision (auto-detected if None)
            client_id: Client identifier to identify the workspace (from config if None)
            zephyr_path: Relative path to Zephyr within workspace (from config if None, default: "zephyr")

        Yields:
            Command output lines, in real time.
        """
        from .detection import detect_west_workspace

        # Auto-detect from current directory if not provided
        if manifest_url is None or manifest_rev is None:
            detected = detect_west_workspace(".")
            if detected:
                manifest_url = manifest_url or detected["manifest_url"]
                manifest_rev = manifest_rev or detected["manifest_rev"]
                yield f"Auto-detected from current directory: {manifest_url} @ {manifest_rev}"

        # Get client_id from config if not provided
        if client_id is None:
            config = self._load_config()
            client_id = config.get("client_id")

        # Get zephyr_path from config if not provided
        zephyr_path = self._get_zephyr_path(zephyr_path)

        # Use default manifest_url if still None
        if manifest_url is None:
            manifest_url = "https://github.com/zephyrproject-rtos/zephyr"

        yield from self.streamingcall("install_sdk", toolchains, manifest_url, manifest_rev, client_id, zephyr_path)

    def flash(
        self,
        operator: Operator,
        path: str,
        manifest_url: str,
        manifest_rev: str,
        client_id: str | None = None,
        zephyr_path: str | None = None,
    ) -> Iterator[str]:
        """Flash firmware using a build directory tar archive

        Args:
            operator: OpenDAL operator for file access
            path: Path to the build directory tar archive
            manifest_url: Git repository URL for the manifest
            manifest_rev: Git revision (tag, branch, or commit hash)
            client_id: Client identifier to identify the workspace
            zephyr_path: Relative path to Zephyr within workspace (from config if None, default: "zephyr")

        Yields:
            Command output lines, in real time.
        """
        # Get zephyr_path from config if not provided
        zephyr_path = self._get_zephyr_path(zephyr_path)

        # The OpendalAdapter must stay open for the whole streaming call so the
        # exporter can pull the archive during the upload phase. Holding the
        # context across ``yield from`` keeps it alive until the generator is
        # exhausted.
        with OpendalAdapter(client=self, operator=operator, path=path) as handle:
            yield from self.streamingcall("flash", handle, manifest_url, manifest_rev, client_id, zephyr_path)

    def flash_build_dir(
        self,
        build_dir: str,
        manifest_url: str | None = None,
        manifest_rev: str | None = None,
        client_id: str | None = None,
        zephyr_path: str | None = None,
    ) -> Iterator[str]:
        """Flash firmware from a local Zephyr build directory

        Packs the build directory into a tar archive and streams it to the
        exporter. The exporter extracts it and runs ``west flash --no-rebuild
        --build-dir``, using the board name and runner configuration embedded
        in the build directory's CMakeCache and runners.yaml.

        Auto-detects manifest parameters from the build directory if not provided.

        Args:
            build_dir: Local path to the Zephyr build directory
                       (e.g., the directory created by ``west build``)
            manifest_url: Git repository URL for the manifest (auto-detected if None)
            manifest_rev: Git revision (auto-detected if None)
            client_id: Client identifier to identify the workspace (from config if None)
            zephyr_path: Relative path to Zephyr within workspace (from config if None, default: "zephyr")

        Yields:
            Command output lines, in real time.
        """
        from .detection import detect_from_build_dir

        # Auto-detect from build directory if not provided
        if manifest_url is None or manifest_rev is None:
            detected = detect_from_build_dir(build_dir)
            if detected:
                manifest_url = manifest_url or detected["manifest_url"]
                manifest_rev = manifest_rev or detected["manifest_rev"]
                yield f"Auto-detected from build directory: {manifest_url} @ {manifest_rev}"
            else:
                raise ValueError(
                    "Could not auto-detect manifest parameters from build directory. "
                    "Please specify --manifest-url and --manifest-rev explicitly."
                )

        # Get client_id from config if not provided
        if client_id is None:
            config = self._load_config()
            client_id = config.get("client_id")

        with tempfile.NamedTemporaryFile(suffix=".tar", delete=True) as tmp:
            with tarfile.open(tmp.name, "w") as tar:
                tar.add(build_dir, arcname=".")

            absolute = Path(tmp.name).resolve()
            yield from self.flash(
                operator=Operator("fs", root="/"),
                path=str(absolute),
                manifest_url=manifest_url,
                manifest_rev=manifest_rev,
                client_id=client_id,
                zephyr_path=zephyr_path,
            )

    def twister(
        self,
        operator: Operator,
        path: str,
        test_roots: list[str],
        manifest_url: str,
        manifest_rev: str,
        client_id: str | None = None,
        zephyr_path: str | None = None,
        extra_twister_args: list[str] | None = None,
    ) -> Iterator[str]:
        """Run twister in test-only mode using a pre-built twister-out archive

        Streams a tar archive to the exporter, which extracts it and runs
        ``west twister --test-only --device-testing``. The updated results are
        kept on the exporter; call ``twister_fetch_results`` to retrieve them.

        Supports any tar format (tar, tar.gz, tar.bz2, tar.xz). The output
        will use the same compression format as the input.

        Args:
            operator: OpenDAL operator for file access
            path: Path to the twister-out tar archive (any compression format)
            test_roots: List of test root paths passed to twister via ``-T``
            manifest_url: Git repository URL for the manifest
            manifest_rev: Git revision (tag, branch, or commit hash)
            client_id: Client identifier to identify the workspace
            zephyr_path: Relative path to Zephyr within workspace (from config if None, default: "zephyr")
            extra_twister_args: Additional arguments to pass to twister (from config if None)

        Yields:
            Command output lines, in real time.
        """
        # Get zephyr_path from config if not provided
        zephyr_path = self._get_zephyr_path(zephyr_path)
        # Get extra_twister_args from config if not provided
        extra_twister_args = self._get_extra_twister_args(extra_twister_args)

        with OpendalAdapter(client=self, operator=operator, path=path) as handle:
            yield from self.streamingcall("twister", handle, test_roots, manifest_url, manifest_rev, client_id, zephyr_path, extra_twister_args)

    def twister_fetch_results(
        self,
        operator: Operator,
        path: str,
        manifest_url: str,
        manifest_rev: str,
        client_id: str | None = None,
        zephyr_path: str | None = None,
    ) -> None:
        """Fetch the twister result archive from the exporter

        Downloads the twister result archive produced by the last ``twister``
        call and writes it to ``path``. The archive format will match the
        format of the input archive.

        Args:
            operator: OpenDAL operator for file access
            path: Local path where the result archive will be written
            manifest_url: Git repository URL for the manifest
            manifest_rev: Git revision (tag, branch, or commit hash)
            client_id: Client identifier to identify the workspace
            zephyr_path: Relative path to Zephyr within workspace (from config if None, default: "zephyr")
        """
        # Get zephyr_path from config if not provided
        zephyr_path = self._get_zephyr_path(zephyr_path)

        with OpendalAdapter(client=self, operator=operator, path=path, mode="wb") as handle:
            self.call("twister_fetch_results", handle, manifest_url, manifest_rev, client_id, zephyr_path)

    def run_twister(
        self,
        archive_path: str,
        test_roots: list[str],
        manifest_url: str,
        manifest_rev: str,
        client_id: str | None = None,
        zephyr_path: str | None = None,
        extra_twister_args: list[str] | None = None,
    ) -> Iterator[str]:
        """Run twister and retrieve results, overwriting the local archive

        Streams the archive to the exporter, runs twister yielding output lines
        as they arrive, then downloads the updated results back, overwriting
        the original archive. The output format will match the input format.

        Args:
            archive_path: Local path to the twister-out tar archive (any compression format)
            test_roots: List of test root paths passed to twister via ``-T``
            manifest_url: Git repository URL for the manifest
            manifest_rev: Git revision (tag, branch, or commit hash)
            client_id: Client identifier to identify the workspace
            zephyr_path: Relative path to Zephyr within workspace (from config if None, default: "zephyr")
            extra_twister_args: Additional arguments to pass to twister (from config if None)

        Yields:
            Command output lines, in real time. After the generator is
            exhausted, ``archive_path`` has been overwritten with the updated
            results in the same compression format.
        """
        absolute = Path(archive_path).resolve()
        tmp_path = absolute.parent / (absolute.name + ".tmp")

        yield from self.twister(
            operator=Operator("fs", root="/"),
            path=str(absolute),
            test_roots=test_roots,
            manifest_url=manifest_url,
            manifest_rev=manifest_rev,
            client_id=client_id,
            zephyr_path=zephyr_path,
            extra_twister_args=extra_twister_args,
        )

        try:
            self.twister_fetch_results(
                operator=Operator("fs", root="/"),
                path=str(tmp_path),
                manifest_url=manifest_url,
                manifest_rev=manifest_rev,
                client_id=client_id,
                zephyr_path=zephyr_path,
            )
            tmp_path.replace(absolute)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    def cli(self):
        @driver_click_group(self)
        def base():
            """West client for remote Zephyr development"""
            pass

        @base.command()
        @click.option(
            "--manifest-url",
            help="Git repository URL (auto-detected from current west workspace if not specified)",
        )
        @click.option(
            "--manifest-rev",
            help="Git revision to checkout (auto-detected from current workspace if not specified)",
        )
        @click.option(
            "--manifest-file",
            help="Manifest file to use (default: west.yml)",
        )
        @click.option(
            "--client-id",
            help="Client identifier to isolate workspaces (from config file if not specified)",
        )
        @click.option(
            "--force",
            is_flag=True,
            help="Force recreate workspace (delete and reinitialize from scratch)",
        )
        @click.option(
            "--zephyr-path",
            help="Relative path to Zephyr within workspace (from config file if not specified, default: zephyr)",
        )
        @click.option(
            "--post-init-command",
            help=(
                "Shell command to run after init completes, on the exporter, with the "
                "workspace venv activated and cwd=workspace_path (or from config file). "
                "Example: 'pip install -r external-module/openthread-tests.git/requirements.txt'"
            ),
        )
        def initialize_workspace(manifest_url, manifest_rev, manifest_file, client_id, force, zephyr_path, post_init_command):
            """Initialize a Zephyr workspace on the exporter"""
            _stream_to_stdout(
                self.initialize_workspace(
                    manifest_url=manifest_url,
                    manifest_rev=manifest_rev,
                    manifest_file=manifest_file,
                    client_id=client_id,
                    force_recreate=force,
                    zephyr_path=zephyr_path,
                    post_init_command=post_init_command,
                )
            )

        @base.command()
        @click.option(
            "--manifest-url",
            help="Git repository URL (auto-detected from current west workspace if not specified)",
        )
        @click.option(
            "--manifest-rev",
            help="Git revision (auto-detected from current workspace if not specified)",
        )
        @click.option(
            "--client-id",
            help="Client identifier to identify the workspace (from config file if not specified)",
        )
        @click.option(
            "--zephyr-path",
            help="Relative path to Zephyr within workspace (from config file if not specified, default: zephyr)",
        )
        def update_workspace(manifest_url, manifest_rev, client_id, zephyr_path):
            """Update workspace dependencies on the exporter"""
            _stream_to_stdout(
                self.update_workspace(
                    manifest_url=manifest_url,
                    manifest_rev=manifest_rev,
                    client_id=client_id,
                    zephyr_path=zephyr_path,
                )
            )

        @base.command()
        @click.option(
            "--toolchains",
            help="Comma-separated list of toolchains to install (e.g., 'arm,riscv')",
        )
        @click.option(
            "--manifest-url",
            help="Git repository URL (auto-detected from current west workspace if not specified)",
        )
        @click.option(
            "--manifest-rev",
            help="Git revision (auto-detected from current workspace if not specified)",
        )
        @click.option(
            "--client-id",
            help="Client identifier to identify the workspace (from config file if not specified)",
        )
        @click.option(
            "--zephyr-path",
            help="Relative path to Zephyr within workspace (from config file if not specified, default: zephyr)",
        )
        def install_sdk(toolchains, manifest_url, manifest_rev, client_id, zephyr_path):
            """Install Zephyr SDK on the exporter"""
            toolchain_list = toolchains.split(",") if toolchains else None
            _stream_to_stdout(
                self.install_sdk(
                    toolchains=toolchain_list,
                    manifest_url=manifest_url,
                    manifest_rev=manifest_rev,
                    client_id=client_id,
                    zephyr_path=zephyr_path,
                )
            )

        @base.command()
        @click.argument("build_dir", type=click.Path(exists=True, file_okay=False, dir_okay=True))
        @click.option(
            "--manifest-url",
            help="Git repository URL (auto-detected from build directory if not specified)",
        )
        @click.option(
            "--manifest-rev",
            help="Git revision (auto-detected from build directory if not specified)",
        )
        @click.option(
            "--client-id",
            help="Client identifier to identify the workspace (from config file if not specified)",
        )
        @click.option(
            "--zephyr-path",
            help="Relative path to Zephyr within workspace (from config file if not specified, default: zephyr)",
        )
        def flash(build_dir, manifest_url, manifest_rev, client_id, zephyr_path):
            """Flash firmware from a Zephyr build directory

            The runner is configured on the exporter via the driver configuration.
            """
            self.logger.info("Flashing from build directory %s...", build_dir)
            _stream_to_stdout(
                self.flash_build_dir(
                    build_dir,
                    manifest_url=manifest_url,
                    manifest_rev=manifest_rev,
                    client_id=client_id,
                    zephyr_path=zephyr_path,
                )
            )

        @base.command()
        @click.argument("archive", type=click.Path(exists=True, file_okay=True, dir_okay=False))
        @click.option(
            "-T",
            "--test-root",
            "test_roots",
            multiple=True,
            required=True,
            help="Test root path (can be specified multiple times)",
        )
        @click.option(
            "--manifest-url",
            required=True,
            help="Git repository URL for the manifest",
        )
        @click.option(
            "--manifest-rev",
            required=True,
            help="Git revision (tag, branch, or commit hash)",
        )
        @click.option(
            "--client-id",
            help="Client identifier to identify the workspace (from config file if not specified)",
        )
        @click.option(
            "--zephyr-path",
            help="Relative path to Zephyr within workspace (from config file if not specified, default: zephyr)",
        )
        @click.option(
            "--extra-twister-arg",
            "extra_twister_args",
            multiple=True,
            help="Additional arguments to pass to twister (can be specified multiple times, or from config file)",
        )
        def twister(archive, test_roots, manifest_url, manifest_rev, client_id, zephyr_path, extra_twister_args):
            """Run twister tests on the exporter using a pre-built archive

            ARCHIVE is the path to the twister-out tar archive produced by the build server.
            Supports any tar format (tar, tar.gz, tar.bz2, tar.xz). The output will use
            the same compression format as the input.

            The hardware map is configured on the exporter via the driver configuration.
            """
            self.logger.info("Running twister from archive %s...", archive)
            _stream_to_stdout(
                self.run_twister(
                    archive,
                    list(test_roots),
                    manifest_url=manifest_url,
                    manifest_rev=manifest_rev,
                    client_id=client_id,
                    zephyr_path=zephyr_path,
                    extra_twister_args=list(extra_twister_args) if extra_twister_args else None,
                )
            )

        return base


def _stream_to_stdout(lines: Iterator[str]) -> None:
    """Print each line as it arrives, flushing so the user sees live progress."""
    for line in lines:
        click.echo(line)
        sys.stdout.flush()
