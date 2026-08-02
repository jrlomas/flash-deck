# Flash Deck User Manual

This manual covers installation and the development, recovery, and repeat-production workflows available in Flash Deck.

## 1. Overview

Flash Deck is a native GTK 4 front end for STMicroelectronics' `STM32_Programmer_CLI`. It uses the CLI, external loaders, device database, and signed ST-LINK updater installed with STM32CubeProgrammer.

Supported transports:

- **ST-LINK:** SWD or JTAG through an ST-LINK/V2, V2-1, or V3 probe.
- **USB DFU:** an STM32 already running a DFU bootloader.
- **UART:** an STM32 accessible through a serial bootloader port.

Target firmware and probe firmware are separate. Target firmware is the image programmed into the STM32. Probe firmware runs inside the ST-LINK debugger. Flash Deck only offers a probe update when ST's updater reports that the selected, connected probe is out of date.

## 2. Safety

- Confirm the selected probe serial before erasing, recovering, or updating.
- Do not disconnect power, USB, or the probe during programming or a probe update.
- Back up important memory before erasing.
- Treat option bytes carefully. Protection, boot, and reset settings can disable normal access.
- Read-unprotect and recovery operations may mass-erase the target.
- Verify target voltage and wiring before connecting to custom hardware.

Flash Deck shows confirmation dialogs before destructive operations. Read them carefully.

## 3. Installation

### Install STM32CubeProgrammer

Install STM32CubeProgrammer for Linux from STMicroelectronics, then confirm:

```bash
~/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI --help
```

### Install the GTK runtime

On Ubuntu:

```bash
sudo apt update
sudo apt install python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
```

Clone and launch:

```bash
git clone https://github.com/jrlomas/flash-deck.git
cd flash-deck
./stm32-flash-deck.py
```

### Install the Ubuntu launcher and icon

From the cloned repository:

```bash
./install.sh
```

This installs the application for the current user under `~/.local/share/flash-deck`, installs the scalable MCU icon into the user icon theme, and creates a matching desktop entry. Launch **Flash Deck** from Ubuntu's application menu so GNOME can associate the running window with its taskbar icon.

Run `./install.sh` again after updating the repository to refresh the installed application.

For a nonstandard CubeProgrammer location:

```bash
STM32_PROGRAMMER_CLI=/opt/st/STM32CubeProgrammer/bin/STM32_Programmer_CLI \
  ./stm32-flash-deck.py
```

Flash Deck derives the external-loader and ST-LINK updater paths from that CLI installation.

### Device permissions

If devices appear only with `sudo`, install ST's USB rules or correct the relevant udev and serial-port permissions. Log out and back in after changing group membership. Do not run the whole graphical application as root merely to bypass permissions.

## 4. Main window

### Profiles

The selector beside **Probes** loads saved setups. The save icon creates or replaces a profile; the delete icon removes one.

### 1 Target

The selector lists discovered ST-LINK, DFU, and usable UART interfaces. The refresh icon rescans. Selected text is shortened with an ellipsis when necessary; opening the menu shows complete entries.

Before connection, the card contains transport-specific settings and **Connect**. After connection, it shows device information and target actions.

Top-right indicators:

- Dim network icon: disconnected
- Bright green network icon: connected
- Bidirectional-arrow action: disconnect
- Update icon, when present: bundled ST-LINK firmware update available

### 2 Firmware

Drop one or more supported images onto the card, or click the drop area to browse. Inspection, address controls, loaders, and programming actions appear after an image is loaded.

### Activity

Activity shows the exact commands and cleaned CLI output. It scrolls horizontally and vertically without resizing the layout.

## 5. Discovering and connecting

Flash Deck scans all supported transports at launch.

1. Open the target selector.
2. Select the intended probe or bootloader.
3. Review the connection settings.
4. Click **Connect**.

Target actions remain hidden until connection succeeds.

### ST-LINK settings

- **Debug port:** SWD or JTAG.
- **Debug speed:** requested probe clock.
- **Connect:** normal, under-reset, hot-plug, power-down, or hardware reset pulse.
- **Access port:** ARM debug access port; normally `0`.
- **Reset mode:** software, hardware, or core reset.
- **Allow shared probe access:** permits shared access where supported.
- **Debug in low-power mode:** retains supported low-power debugging behavior.
- **SWD multidrop:** optional `TargetSel` for multidrop systems.

Start with SWD, a conservative clock, normal connection, access port 0, and software reset. Try under-reset if target firmware prevents normal connection.

### USB DFU

DFU entries identify a USB serial and use the displayed VID/PID. The STM32 must already be in DFU mode.

### UART

Choose the baud rate, parity, and stop bits required by the STM32 ROM bootloader and board wiring. Flash Deck excludes ordinary built-in `ttyS` ports from automatic results.

## 6. Firmware jobs

Supported extensions:

- `.bin`
- `.hex`
- `.elf`
- `.srec`
- `.s19`
- `.stm32`

Drop multiple files to build a multi-image job. Each row shows format, size, SHA-256 prefix, and an address range when available.

### Addresses

BIN files contain no address and require a start address, commonly `0x08000000` for internal flash. HEX, ELF, and S-record files normally contain addresses.

Flash Deck reports invalid addresses, overlapping images, missing files, and ranges outside the connected target's known memory map.

### Flash settings

- **Default binary address:** initial address for newly loaded BIN files.
- **Program only modified sectors:** limits work where supported.
- **Skip erase before programming:** omits automatic erase; use only when destination state permits it.
- **Fast sector verification:** uses the optimized verification workflow.
- **Reset target after flashing:** resets after a successful job.

### External loaders

Use **External loader…** for QSPI, OSPI, NAND, or other external memory supported by an ST `.stldr` file. Use only a loader matching the exact target and memory hardware.

## 7. Programming

### Flash & verify

Programs every job image and verifies the result. It is enabled only when a target is connected and the job validates.

### Verify only

Reads the target and compares every addressable byte without programming. BIN, HEX, S-record, and ELF jobs are supported. Proprietary or encrypted STM32 containers may not support read-only verification.

### Erase chip

Performs a full programmable-flash erase after confirmation.

Operations stream into Activity. Success status is green; command or validation problems use warning status.

## 8. Connected-target tools

### Examine memory

The memory inspector provides:

- Device-aware memory regions
- Address, page-size, and previous/next controls
- Editable hexadecimal display
- Binary import and export
- Checksum and blank check
- Repeated 8-, 16-, or 32-bit fill
- Direct 8-, 16-, or 32-bit writes
- Selective sector erase

To edit:

1. Choose a region, address, and size.
2. Click **Read page**.
3. Edit hexadecimal byte pairs.
4. Click **Write edits…**.
5. Review the changed-byte count and address, then confirm.

Flash writes may require erased bits. Import, fill, direct-write, and sector-erase operations have their own confirmations.

### Option bytes

**Option bytes…** parses the CLI report into controls:

- Recognized binary fields use switches.
- Other hexadecimal fields use validated entries.
- Symbolic or read-only values use labels.

Only changed editable fields are written. **Save changes** summarizes and confirms the changes. Option availability varies by STM32 family; consult the device reference manual before changing protection or boot fields.

### Target controls

Available controls include system reset, hard reset, halt, run, single step, core status, and run from an address. Debug-only actions are disabled for transports that cannot support them.

### Recovery

Recovery includes guarded operations for bad option-byte state and read protection. Removing read protection normally mass-erases the chip. A second confirmation is required.

## 9. ST-LINK firmware updates

Flash Deck uses the signed updater bundled with the selected CubeProgrammer installation.

1. Select and connect through an ST-LINK.
2. Flash Deck checks that exact serial.
3. If a newer compatible bundled version exists, an update icon appears left of Disconnect and the green connection icon.
4. Click it and confirm the displayed board, serial, and current firmware.
5. Click **Update** and do not unplug the probe.

Choosing **No** closes the dialog without action. During updates, conflicting actions are disabled and progress appears in Activity. The probe is rediscovered after success because it re-enumerates over USB.

No icon means the probe is current, unsupported by the bundle, unavailable to the updater, or the connection is not ST-LINK. Flash Deck never uses the updater's ambiguous first-probe default; every check and update includes the selected serial.

To obtain newer probe firmware, update STM32CubeProgrammer first.

## 10. Profiles

A profile saves target identity, transport settings, image paths and addresses, external loaders, flash settings, and production defaults.

To save:

1. Configure the target and firmware job.
2. Click the save icon beside the profile selector.
3. Enter a name and confirm.

Selecting a profile restores available files and settings and selects its target if present. Missing files are reported.

Profiles are stored at:

```text
~/.config/flash-deck/profiles.json
```

Deleting a profile never deletes firmware files.

## 11. Production mode

Production mode repeatedly programs and verifies equivalent units.

1. Connect a representative target.
2. Load and validate the job.
3. Open **Production mode…**.
4. Optionally require a new scanned unit serial each cycle.
5. Optionally set the UID address.
6. Click **Start production run**.
7. Follow the state message; remove each unit after PASS or FAIL.

Removal gating prevents counting one attached target twice. Pass, fail, and total counters update throughout the run.

History is stored at:

```text
~/.local/share/flash-deck/production-history.csv
```

Records include timestamp, result, unit serial, UID when available, device/probe identifiers, profile, image/job hashes, and duration. Export CSV or JSON from the production window.

## 12. Troubleshooting

### CLI not found

Confirm `STM32_Programmer_CLI` exists and is executable, or use the environment override in section 3.

### No devices found

- Reconnect USB and refresh.
- Confirm ST's udev rules.
- Check cables, power, and USB hubs.
- Ensure a UART port is not open elsewhere.
- Confirm an intended DFU target is actually in DFU mode.

### ST-LINK connection fails

- Verify the selected serial.
- Reduce debug speed.
- Try under-reset.
- Check voltage and SWD/JTAG wiring.
- Disable shared access unless required.
- Close other software using that probe.

### Firmware actions remain disabled

A successful connection and valid firmware job are both required. Read the warning under Flash settings and inspect Activity.

### No probe-update icon

The icon appears only when ST's bundled updater reports an update for the exact connected ST-LINK. An up-to-date probe correctly shows no icon.

### Option-byte fields are missing or read-only

Fields depend on the family and CLI output. Symbolic values without a safe writable representation are intentionally read-only. Activity retains the complete cleaned CLI output.

### Profile files moved

Profiles contain absolute firmware and loader paths. Restore those paths or reload the files and save the profile again.

## 13. Data and boundaries

Flash Deck runs local ST tools and does not upload firmware, memory, profiles, or production history. GitHub is used only to obtain this source repository.

STM32CubeProgrammer, ST device databases, probe firmware bundles, and external loaders are not redistributed here. Their installation and licensing remain separate.
