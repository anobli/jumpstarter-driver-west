# Copyright (c) 2026 BayLibre
# SPDX-License-Identifier: Apache-2.0

import hashlib
import urllib.parse


def generate_workspace_id(manifest_url: str, manifest_rev: str, client_id: str | None = None) -> str:
    """Generate a stable, filesystem-safe workspace identifier

    The workspace ID is constructed from the repository name, manifest revision,
    and optional client identifier. This allows multiple users and CI systems to
    work with the same manifest without conflicts.

    Args:
        manifest_url: Git repository URL (e.g., "https://github.com/zephyrproject-rtos/zephyr")
        manifest_rev: Git revision (tag, branch, commit hash)
        client_id: Optional client identifier to prevent conflicts between users/CI

    Returns:
        Filesystem-safe workspace identifier

    Examples:
        >>> generate_workspace_id("https://github.com/zephyrproject-rtos/zephyr", "v3.5.0")
        'zephyr_v3.5.0'
        >>> generate_workspace_id("https://github.com/zephyrproject-rtos/zephyr", "v3.5.0", "dev-alice")
        'zephyr_v3.5.0_dev-alice'
        >>> generate_workspace_id("https://github.com/zephyrproject-rtos/zephyr", "main", "ci")
        'zephyr_main_ci'
    """
    # Parse repository name from URL
    parsed = urllib.parse.urlparse(manifest_url)
    repo_path = parsed.path.rstrip('/')
    repo_name = repo_path.split('/')[-1].replace('.git', '')

    # Sanitize revision to be filesystem-safe
    clean_rev = manifest_rev.replace('refs/heads/', '').replace('refs/tags/', '')
    clean_rev = clean_rev.replace('/', '-')  # Replace slashes with dashes

    # Build components
    components = [repo_name, clean_rev]
    if client_id:
        # Sanitize client_id as well
        clean_client_id = client_id.replace('/', '-').replace(' ', '-')
        components.append(clean_client_id)

    workspace_id = '_'.join(components)

    # If the ID is too long for most filesystems (>200 chars), hash it
    if len(workspace_id) > 200:
        hash_suffix = hashlib.sha256(workspace_id.encode()).hexdigest()[:12]
        workspace_id = f"{repo_name}_{hash_suffix}"

    return workspace_id
