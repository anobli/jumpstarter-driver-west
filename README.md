# West Driver

`jumpstarter-driver-west` enables remote Zephyr RTOS development by providing access to the West tool on remote hardware.

## Overview

The West driver allows you to use standard Zephyr tools (west, and in the future, twister) from your local development machine to interact with hardware located on a remote exporter. This enables:

- **Remote flashing**: Flash firmware to boards anywhere in your office
- **Remote debugging**: Debug hardware without physical access
- **Remote testing**: Run tests on real hardware from your desk
- **Workspace management**: Set up and maintain Zephyr environments on exporters

You build and develop locally, but the actual hardware interaction (flashing, debugging) happens remotely. The build directory is transferred to the exporter where West executes the flash operation with the correct runner configuration.

## Installation

```{code-block} console
:substitutions:
$ pip3 install --extra-index-url {{index_url}} jumpstarter-driver-west
```

## Prerequisites

On the exporter machine:

1. **West tool**: The Zephyr West command-line tool must be installed
2. **Git**: Required for workspace initialization
3. **Hardware access**: Appropriate debug probes (JTAG/SWD) connected to target boards

The Zephyr workspace and SDK can be set up remotely using the driver's management commands.

## Configuration

### Minimal Configuration

The minimal configuration only requires the workspace path:

```yaml
export:
  west:
    type: jumpstarter_driver_west.driver.West
    config:
      workspace_path: "/home/exporter/zephyr-workspace"
```

With this configuration, you can use the `initialize_workspace` and `install_sdk` commands to set up the environment remotely.

### Full Configuration

For a fully configured setup with runner specification:

```yaml
export:
  west:
    type: jumpstarter_driver_west.driver.West
    config:
      # Required: workspace directory
      workspace_path: "/home/exporter/zephyr-workspace"

      # Optional: custom Zephyr location (defaults to {workspace_path}/zephyr)
      zephyr_base: "/custom/path/to/zephyr"

      # Optional: SDK path (can be installed remotely)
      sdk_path: "/opt/zephyr-sdk-0.16.5"

      # Optional: flash runner and arguments
      # The runner is controlled by the exporter admin
      extra_flash_args:
        - "--runner"
        - "openocd"  # or "jlink", "pyocd", etc.
```

### Configuration Parameters

- **workspace_path** (required): Path to the West workspace directory on the exporter. You can have multiple workspaces to avoid conflicts between different versions.

- **zephyr_base** (optional): Path to the Zephyr source tree. If not specified, defaults to `{workspace_path}/zephyr`.

- **sdk_path** (optional): Path to the Zephyr SDK installation. Can be installed using the `install_sdk` command.

- **extra_flash_args** (optional): Additional arguments passed to `west flash`. Use this to specify the runner and runner-specific options. **Note**: The runner configuration is controlled by the exporter administrator, not by developers.

## Usage

### Setting Up the Workspace

Before flashing firmware, you need to initialize the Zephyr workspace on the exporter:

#### Initialize Workspace (CLI)

```bash
# Connect to the exporter
jmp shell <exporter-name>

# Initialize with latest Zephyr
j west initialize-workspace

# Initialize with a specific version
j west initialize-workspace --manifest-rev v3.5.0

# Initialize with a custom manifest
j west initialize-workspace \
  --manifest-url https://github.com/myorg/zephyr \
  --manifest-rev main
```

#### Initialize Workspace (Python)

```python
with jumpstarter.env() as client:
    west = client.west

    # Initialize with latest Zephyr
    west.initialize_workspace()

    # Initialize with specific version
    west.initialize_workspace(manifest_rev="v3.5.0")
```

### Installing the Zephyr SDK

If `sdk_path` is configured, you can install the SDK remotely:

#### Install SDK (CLI)

```bash
# Install all toolchains
j west install-sdk

# Install specific toolchains only
j west install-sdk --toolchains arm,riscv
```

#### Install SDK (Python)

```python
with jumpstarter.env() as client:
    west = client.west

    # Install all toolchains
    west.install_sdk()

    # Install specific toolchains
    west.install_sdk(toolchains=["arm", "riscv"])
```

### Updating the Workspace

Update all workspace dependencies (equivalent to `west update`):

```bash
j west update-workspace
```

### Flashing Firmware

Once the workspace is set up, you can flash firmware from your local build directory:

#### Flash (CLI)

```bash
# Flash from local build directory
j west flash /path/to/build
```

The runner is configured in the exporter's driver configuration, ensuring developers use the correct hardware setup.

#### Flash (Python)

```python
with jumpstarter.env() as client:
    west = client.west

    # Flash firmware from a local build directory
    west.flash_build_dir("/path/to/build")
```

## Complete Workflow Example

Here's a complete example of setting up and using a remote Zephyr development environment:

```bash
# 1. Connect to the exporter
jmp shell my-zephyr-board

# 2. Initialize the workspace (first time only)
j west initialize-workspace --manifest-rev v3.5.0

# 3. Install the SDK (first time only, if sdk_path is configured)
j west install-sdk --toolchains arm

# 4. Build locally on your development machine (outside jmp shell)
cd ~/my-zephyr-project
west build -b nrf52840dk_nrf52840 samples/hello_world

# 5. Back in jmp shell, flash to the remote board
j west flash ~/my-zephyr-project/build

# 6. Later, update to a new Zephyr version
j west initialize-workspace --manifest-rev v3.6.0
j west update-workspace
```

## Runner Configuration

The debug runner (OpenOCD, J-Link, pyOCD, etc.) is configured by the exporter administrator via `extra_flash_args`, not by developers at runtime. This ensures:

- **Consistency**: Everyone uses the correct runner for the hardware
- **Hardware protection**: Prevents incorrect configurations that could damage hardware
- **Access control**: The exporter admin controls which debug interfaces are available

Example runner configurations:

```yaml
# OpenOCD
extra_flash_args:
  - "--runner"
  - "openocd"

# J-Link
extra_flash_args:
  - "--runner"
  - "jlink"

# PyOCD
extra_flash_args:
  - "--runner"
  - "pyocd"

# OpenOCD with custom configuration
extra_flash_args:
  - "--runner"
  - "openocd"
  - "--openocd"
  - "/custom/path/to/openocd"
  - "--openocd-search"
  - "/custom/scripts/path"
```

## Python API Reference

```python
class WestClient:
    def initialize_workspace(
        self,
        manifest_url: str = "https://github.com/zephyrproject-rtos/zephyr",
        manifest_rev: str | None = None,
        manifest_file: str | None = None,
    ) -> str:
        """Initialize a Zephyr workspace on the exporter"""

    def update_workspace(self) -> str:
        """Update workspace dependencies"""

    def install_sdk(self, toolchains: list[str] | None = None) -> str:
        """Install Zephyr SDK"""

    def flash_build_dir(self, build_dir: str) -> str:
        """Flash firmware from a local build directory"""
```

## Future Features

- **Remote Twister integration**: Run Zephyr test suites on remote hardware
- **Remote debugging**: Attach debuggers to remote targets
- **Build server support**: Build and test on dedicated build servers with the same workspace management
