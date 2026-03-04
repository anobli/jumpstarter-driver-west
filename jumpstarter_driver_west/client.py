# Copyright (c) 2026 BayLibre
# SPDX-License-Identifier: Apache-2.0

import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

import click
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
    """

    def initialize_workspace(
        self,
        manifest_url: str = "https://github.com/zephyrproject-rtos/zephyr",
        manifest_rev: str | None = None,
        manifest_file: str | None = None,
    ) -> str:
        """Initialize a Zephyr workspace on the exporter

        Args:
            manifest_url: Git repository URL for the manifest
            manifest_rev: Git revision to checkout (tag, branch, or commit)
            manifest_file: Manifest file to use (default: west.yml)

        Returns:
            Command output
        """
        return self.call(
            "initialize_workspace",
            manifest_url,
            manifest_rev,
            manifest_file,
        )

    def update_workspace(self) -> str:
        """Update workspace dependencies on the exporter

        Returns:
            Command output
        """
        return self.call("update_workspace")

    def install_sdk(self, toolchains: list[str] | None = None) -> str:
        """Install Zephyr SDK on the exporter

        Args:
            toolchains: List of specific toolchains to install

        Returns:
            Command output
        """
        return self.call("install_sdk", toolchains)

    def flash(self, operator: Operator, path: str) -> str:
        """Flash firmware using a build directory tar archive

        Args:
            operator: OpenDAL operator for file access
            path: Path to the build directory tar archive

        Returns:
            Command output
        """
        with OpendalAdapter(client=self, operator=operator, path=path) as handle:
            return self.call("flash", handle)

    def flash_build_dir(self, build_dir: str) -> str:
        """Flash firmware from a local Zephyr build directory

        Packs the build directory into a tar archive and streams it to the
        exporter. The exporter extracts it and runs ``west flash --no-rebuild
        --build-dir``, using the board name and runner configuration embedded
        in the build directory's CMakeCache and runners.yaml.

        Args:
            build_dir: Local path to the Zephyr build directory
                       (e.g., the directory created by ``west build``)

        Returns:
            Command output
        """
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=True) as tmp:
            with tarfile.open(tmp.name, "w") as tar:
                tar.add(build_dir, arcname=".")

            absolute = Path(tmp.name).resolve()
            return self.flash(
                operator=Operator("fs", root="/"),
                path=str(absolute),
            )

    def twister(self, operator: Operator, path: str, test_roots: list[str]) -> str:
        """Run twister in test-only mode using a pre-built twister-out archive

        Streams a tar.gz archive to the exporter, which extracts it and runs
        ``west twister --test-only --device-testing``. The updated results are
        kept on the exporter; call ``twister_fetch_results`` to retrieve them.

        Args:
            operator: OpenDAL operator for file access
            path: Path to the twister-out tar.gz archive
            test_roots: List of test root paths passed to twister via ``-T``

        Returns:
            Command output
        """
        with OpendalAdapter(client=self, operator=operator, path=path) as handle:
            return self.call("twister", handle, test_roots)

    def twister_fetch_results(self, operator: Operator, path: str) -> None:
        """Fetch the twister result archive from the exporter

        Downloads the ``twister-out-result.tar.gz`` produced by the last
        ``twister`` call and writes it to ``path``.

        Args:
            operator: OpenDAL operator for file access
            path: Local path where the result archive will be written
        """
        with OpendalAdapter(client=self, operator=operator, path=path, mode="wb") as handle:
            self.call("twister_fetch_results", handle)

    def run_twister(self, archive_path: str, test_roots: list[str]) -> str:
        """Run twister and retrieve results, overwriting the local archive

        Streams the archive to the exporter, runs twister, then downloads the
        updated results back, overwriting the original archive.

        Args:
            archive_path: Local path to the twister-out tar.gz archive
            test_roots: List of test root paths passed to twister via ``-T``

        Returns:
            Command output
        """
        absolute = Path(archive_path).resolve()
        tmp_path = absolute.parent / (absolute.name + ".tmp")
        output = self.twister(
            operator=Operator("fs", root="/"),
            path=str(absolute),
            test_roots=test_roots,
        )
        try:
            self.twister_fetch_results(
                operator=Operator("fs", root="/"),
                path=str(tmp_path),
            )
            tmp_path.replace(absolute)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise
        return output

    def cli(self):
        @driver_click_group(self)
        def base():
            """West client for remote Zephyr development"""
            pass

        @base.command()
        @click.option(
            "--manifest-url",
            default="https://github.com/zephyrproject-rtos/zephyr",
            help="Git repository URL for the manifest",
        )
        @click.option(
            "--manifest-rev",
            help="Git revision to checkout (tag, branch, or commit)",
        )
        @click.option(
            "--manifest-file",
            help="Manifest file to use (default: west.yml)",
        )
        def initialize_workspace(manifest_url, manifest_rev, manifest_file):
            """Initialize a Zephyr workspace on the exporter"""
            self.logger.info("Initializing workspace...")
            result = self.initialize_workspace(
                manifest_url=manifest_url,
                manifest_rev=manifest_rev,
                manifest_file=manifest_file,
            )
            print(result)

        @base.command()
        def update_workspace():
            """Update workspace dependencies on the exporter"""
            self.logger.info("Updating workspace...")
            result = self.update_workspace()
            print(result)

        @base.command()
        @click.option(
            "--toolchains",
            help="Comma-separated list of toolchains to install (e.g., 'arm,riscv')",
        )
        def install_sdk(toolchains):
            """Install Zephyr SDK on the exporter"""
            self.logger.info("Installing SDK...")
            toolchain_list = toolchains.split(",") if toolchains else None
            result = self.install_sdk(toolchains=toolchain_list)
            print(result)

        @base.command()
        @click.argument("build_dir", type=click.Path(exists=True, file_okay=False, dir_okay=True))
        def flash(build_dir):
            """Flash firmware from a Zephyr build directory

            The runner is configured on the exporter via the driver configuration.
            """
            self.logger.info("Flashing from build directory %s...", build_dir)
            result = self.flash_build_dir(build_dir)
            print(result)

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
        def twister(archive, test_roots):
            """Run twister tests on the exporter using a pre-built archive

            ARCHIVE is the path to the twister-out tar.gz produced by the build server.

            The hardware map is configured on the exporter via the driver configuration.
            """
            self.logger.info("Running twister from archive %s...", archive)
            result = self.run_twister(archive, list(test_roots))
            print(result)

        return base
