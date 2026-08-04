# Flash Deck

Flash Deck is a modern GTK 4 interface for everyday STM32 programming. It wraps STMicroelectronics' installed `STM32_Programmer_CLI`, keeping ST's device support and flash loaders while replacing the Java GUI with a fast, native Ubuntu application.

## Highlights

- Automatic ST-LINK, USB DFU, and UART discovery
- Explicit connect/disconnect lifecycle with live target information
- Drag-and-drop BIN, Intel HEX, ELF, S-record, and STM32 firmware jobs
- Multi-image flashing, exact verification, erase, reset, and external loaders
- Device memory inspector with editable hex, import/export, fill, checksum, and blank check
- Parsed option-byte controls with validation and guarded writes
- Serial-locked ST-LINK firmware checks and updates using ST's bundled updater
- Saved flashing profiles and repeat/production mode with CSV/JSON history
- Streaming, copyable, two-axis-scrollable CLI activity output

## Requirements

- Ubuntu or another Linux desktop with GTK 4 and libadwaita
- Python 3.10 or newer
- STM32CubeProgrammer installed locally
- Permission to access the relevant USB or serial devices

On Ubuntu, install the GUI runtime dependencies with:

```bash
sudo apt install python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
```

STM32CubeProgrammer is not redistributed by this repository. Install it from STMicroelectronics before running Flash Deck.

## Quick start

```bash
git clone https://github.com/jrlomas/flash-deck.git
cd flash-deck
./stm32-flash-deck.py
```

To install Flash Deck in your Ubuntu application menu with its MCU icon:

```bash
./install.sh
```

Launch **Flash Deck** from the application menu. The installed desktop entry and GTK application ID match, so Ubuntu uses the Flash Deck icon in the dock, taskbar, and application overview.

Flash Deck automatically checks:

```text
~/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI
```

For a different installation:

```bash
STM32_PROGRAMMER_CLI=/path/to/STM32_Programmer_CLI ./stm32-flash-deck.py
```

Then:

1. Select a discovered target.
2. Choose the connection settings and click **Connect**.
3. Drop a firmware image onto the Firmware card.
4. Review its address and flash settings.
5. Click **Flash & verify**.

## Documentation

See the [Flash Deck User Manual](docs/USER_MANUAL.md) for complete operating instructions, safety notes, production workflows, and troubleshooting.

## ST-LINK/V2 clone serial repair

The experimental `scripts/stlink-v2-clone-serial.zsh` utility can assign a
unique 24-character hexadecimal serial to a standalone ST-LINK/V2 clone while
preserving compatibility with CubeProgrammer and OpenOCD. It isolates one exact
USB topology with `bwrap`, verifies DFU v1 before writing, and requires explicit
confirmation. Run the script with `--help` for the guarded workflow.

The utility does not redistribute ST firmware. It builds a private updater from
the locally installed CubeProgrammer package and keeps generated artifacts in
the ignored `build/` directory. It also requires
[`lujji/st-decrypt`](https://github.com/lujji/st-decrypt), either cloned under
`tools/st-decrypt` or supplied through `STLINK_DECRYPTOR_JAR`.

## Local data

- Profiles: `~/.config/flash-deck/profiles.json`
- Production history: `~/.local/share/flash-deck/production-history.csv`

## Design

Flash Deck is intentionally a GUI around ST's command-line tools, not a reimplementation of ST-LINK protocols or device-specific flash loaders. Commands and results are visible in Activity so operations remain inspectable.
