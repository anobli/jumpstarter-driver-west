# Copyright (c) 2026 BayLibre
# SPDX-License-Identifier: Apache-2.0

import configparser
import os
import subprocess
from pathlib import Path


def detect_west_workspace(start_path: str = ".") -> dict | None:
    """Detect west workspace by searching upward for .west/config

    Searches from start_path upward through parent directories to find a
    .west/config file, then extracts the manifest repository information.

    Args:
        start_path: Directory to start searching from (default: current directory)

    Returns:
        Dictionary with "manifest_url" and "manifest_rev" keys, or None if not found

    Example:
        >>> info = detect_west_workspace()
        >>> if info:
        ...     print(f"Found workspace: {info['manifest_url']} @ {info['manifest_rev']}")
    """
    current = Path(start_path).resolve()

    # Search upward for .west directory
    for parent in [current] + list(current.parents):
        west_config = parent / ".west" / "config"
        if west_config.exists():
            return _parse_west_config(str(west_config), str(parent))

    return None


def detect_from_build_dir(build_dir: str) -> dict | None:
    """Detect workspace from a Zephyr build directory

    Reads the CMakeCache.txt file to find ZEPHYR_BASE, then searches for the
    west workspace containing that Zephyr installation.

    Args:
        build_dir: Path to Zephyr build directory (containing CMakeCache.txt)

    Returns:
        Dictionary with "manifest_url" and "manifest_rev" keys, or None if not found

    Example:
        >>> info = detect_from_build_dir("/path/to/build")
        >>> if info:
        ...     print(f"Build uses: {info['manifest_url']} @ {info['manifest_rev']}")
    """
    cmake_cache = Path(build_dir) / "CMakeCache.txt"
    if not cmake_cache.exists():
        return None

    # Parse CMakeCache to find ZEPHYR_BASE
    zephyr_base = None
    try:
        with open(cmake_cache) as f:
            for line in f:
                if line.startswith("ZEPHYR_BASE:"):
                    # Format: ZEPHYR_BASE:PATH=/path/to/zephyr
                    zephyr_base = line.split("=", 1)[1].strip()
                    break
    except (OSError, IOError):
        return None

    if not zephyr_base:
        return None

    # Find workspace by looking for .west in parent directories of ZEPHYR_BASE
    return detect_west_workspace(zephyr_base)


def _parse_west_config(config_path: str, workspace_path: str) -> dict | None:
    """Parse .west/config to extract manifest repository information

    West config is an INI-like format:
    [manifest]
    path = zephyr
    file = west.yml

    Args:
        config_path: Path to .west/config file
        workspace_path: Path to the workspace directory

    Returns:
        Dictionary with "manifest_url" and "manifest_rev" keys, or None on error
    """
    try:
        config = configparser.ConfigParser()
        config.read(config_path)

        # Get manifest path from config
        manifest_path = config.get("manifest", "path", fallback="zephyr")
        manifest_repo_path = Path(workspace_path) / manifest_path

        if not manifest_repo_path.exists():
            return None

        # Extract git info from the manifest repository
        return _get_git_info(str(manifest_repo_path))

    except (configparser.Error, OSError, IOError):
        return None


def _get_git_info(repo_path: str) -> dict | None:
    """Extract git remote URL and current revision from a repository

    Runs git commands to get the remote URL and current HEAD reference.

    Args:
        repo_path: Path to git repository

    Returns:
        Dictionary with "manifest_url" and "manifest_rev" keys, or None on error
    """
    try:
        # Get remote URL
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        manifest_url = result.stdout.strip()

        # Get current revision (branch name or tag)
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        manifest_rev = result.stdout.strip()

        # If detached HEAD, try to get tag
        if manifest_rev == "HEAD":
            result = subprocess.run(
                ["git", "describe", "--tags", "--exact-match"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                manifest_rev = result.stdout.strip()
            else:
                # Fall back to short commit SHA
                result = subprocess.run(
                    ["git", "rev-parse", "--short=12", "HEAD"],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                manifest_rev = result.stdout.strip()

        return {
            "manifest_url": manifest_url,
            "manifest_rev": manifest_rev,
        }

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
