# Copyright (c) 2026 BayLibre
# SPDX-License-Identifier: Apache-2.0

import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

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
    - Run twister tests on remote hardware

    All long-running operations are exposed as iterators of output lines so the
    caller can render them live. The CLI commands print each line as it arrives;
    Python callers can do the same or collect them into a string.
    """

    def initialize_workspace(
        self,
        manifest_url: str = "https://github.com/zephyrproject-rtos/zephyr",
        manifest_rev: str | None = None,
        manifest_file: str | None = None,
    ) -> Iterator[str]:
        """Initialize a Zephyr workspace on the exporter

        Args:
            manifest_url: Git repository URL for the manifest
            manifest_rev: Git revision to checkout (tag, branch, or commit)
            manifest_file: Manifest file to use (default: west.yml)

        Yields:
            Command output lines, in real time.
        """
        return self.streamingcall(
            "initialize_workspace",
            manifest_url,
            manifest_rev,
            manifest_file,
        )

    def update_workspace(self) -> Iterator[str]:
        """Update workspace dependencies on the exporter

        Yields:
            Command output lines, in real time.
        """
        return self.streamingcall("update_workspace")

    def install_sdk(self, toolchains: list[str] | None = None) -> Iterator[str]:
        """Install Zephyr SDK on the exporter

        Args:
            toolchains: List of specific toolchains to install

        Yields:
            Command output lines, in real time.
        """
        return self.streamingcall("install_sdk", toolchains)

    def flash(self, operator: Operator, path: str) -> Iterator[str]:
        """Flash firmware using a build directory tar archive

        Args:
            operator: OpenDAL operator for file access
            path: Path to the build directory tar archive

        Yields:
            Command output lines, in real time.
        """
        # The OpendalAdapter must stay open for the whole streaming call so the
        # exporter can pull the archive during the upload phase. Holding the
        # context across ``yield from`` keeps it alive until the generator is
        # exhausted.
        with OpendalAdapter(client=self, operator=operator, path=path) as handle:
            yield from self.streamingcall("flash", handle)

    def flash_build_dir(self, build_dir: str) -> Iterator[str]:
        """Flash firmware from a local Zephyr build directory

        Packs the build directory into a tar archive and streams it to the
        exporter. The exporter extracts it and runs ``west flash --no-rebuild
        --build-dir``, using the board name and runner configuration embedded
        in the build directory's CMakeCache and runners.yaml.

        Args:
            build_dir: Local path to the Zephyr build directory
                       (e.g., the directory created by ``west build``)

        Yields:
            Command output lines, in real time.
        """
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=True) as tmp:
            with tarfile.open(tmp.name, "w") as tar:
                tar.add(build_dir, arcname=".")

            absolute = Path(tmp.name).resolve()
            yield from self.flash(
                operator=Operator("fs", root="/"),
                path=str(absolute),
            )

    def twister(
        self, operator: Operator, path: str, test_roots: list[str]
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

        Yields:
            Command output lines, in real time.
        """
        with OpendalAdapter(client=self, operator=operator, path=path) as handle:
            yield from self.streamingcall("twister", handle, test_roots)

    def twister_fetch_results(self, operator: Operator, path: str) -> None:
        """Fetch the twister result archive from the exporter

        Downloads the twister result archive produced by the last ``twister``
        call and writes it to ``path``. The archive format will match the
        format of the input archive.

        Args:
            operator: OpenDAL operator for file access
            path: Local path where the result archive will be written
        """
        with OpendalAdapter(client=self, operator=operator, path=path, mode="wb") as handle:
            self.call("twister_fetch_results", handle)

    def run_twister(self, archive_path: str, test_roots: list[str]) -> Iterator[str]:
        """Run twister and retrieve results, overwriting the local archive

        Streams the archive to the exporter, runs twister yielding output lines
        as they arrive, then downloads the updated results back, overwriting
        the original archive. The output format will match the input format.

        Args:
            archive_path: Local path to the twister-out tar archive (any compression format)
            test_roots: List of test root paths passed to twister via ``-T``

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
            _stream_to_stdout(
                self.initialize_workspace(
                    manifest_url=manifest_url,
                    manifest_rev=manifest_rev,
                    manifest_file=manifest_file,
                )
            )

        @base.command()
        def update_workspace():
            """Update workspace dependencies on the exporter"""
            _stream_to_stdout(self.update_workspace())

        @base.command()
        @click.option(
            "--toolchains",
            help="Comma-separated list of toolchains to install (e.g., 'arm,riscv')",
        )
        def install_sdk(toolchains):
            """Install Zephyr SDK on the exporter"""
            toolchain_list = toolchains.split(",") if toolchains else None
            _stream_to_stdout(self.install_sdk(toolchains=toolchain_list))

        @base.command()
        @click.argument("build_dir", type=click.Path(exists=True, file_okay=False, dir_okay=True))
        def flash(build_dir):
            """Flash firmware from a Zephyr build directory

            The runner is configured on the exporter via the driver configuration.
            """
            self.logger.info("Flashing from build directory %s...", build_dir)
            _stream_to_stdout(self.flash_build_dir(build_dir))

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

            ARCHIVE is the path to the twister-out tar archive produced by the build server.
            Supports any tar format (tar, tar.gz, tar.bz2, tar.xz). The output will use
            the same compression format as the input.

            The hardware map is configured on the exporter via the driver configuration.
            """
            self.logger.info("Running twister from archive %s...", archive)
            _stream_to_stdout(self.run_twister(archive, list(test_roots)))

        return base


def _stream_to_stdout(lines: Iterator[str]) -> None:
    """Print each line as it arrives, flushing so the user sees live progress."""
    for line in lines:
        click.echo(line)
        sys.stdout.flush()
