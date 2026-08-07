# Guarded ST-LINK/V2 clone bootloader serial repair

This directory contains the reproducible tooling used to give the tested
standalone ST-LINK/V2 clone one USB identity in both loader and application
mode. It intentionally supports only the exact 16 KiB loader whose SHA-256 is
`510909fd7f2a85a0b3f9fed6c41a6dfc4bb9925f6b084fd94ef8715958334481`.
Unknown firmware fails closed.

This operation rewrites two pages inside the protected ST-LINK bootloader and
can brick a probe. Use a sacrificial clone first. Never use a target connector
as a recovery assumption; a failed loader patch requires external SWD access
to the probe MCU.

## Dependencies

- Python 3 and PyUSB
- ARM GNU toolchain
- a built [libopencm3](https://github.com/libopencm3/libopencm3) checkout
- [stlink-tool](https://github.com/jeanthom/stlink-tool)
- `bwrap` for exact-topology USB isolation

## Per-probe workflow

Set a unique 12-character hexadecimal serial and use an ignored build
directory:

```sh
serial=F1A5DEC00001
topology=1-8.2
work=build/stlink-bootloader/$serial
mkdir -p "$work"
```

Build the read-only dumper:

```sh
make -C tools/stlink-v2-bootloader \
  LIBOPENCM3=/path/to/libopencm3 BUILD="$PWD/$work" \
  "$PWD/$work/bootdump.bin"
```

With the probe in its updater loader, upload `bootdump.bin`. The wrapper
isolates the exact topology with `bwrap`; set `STLINK_TOOL` if the executable
is not on `PATH`:

```sh
STLINK_TOOL=/path/to/stlink-tool \
  scripts/stlink-v2-clone-serial.zsh load "$topology" "$work/bootdump.bin"
tools/stlink-v2-bootloader/capture.py --topology "$topology" "$work/bootloader.1.bin"
tools/stlink-v2-bootloader/capture.py --topology "$topology" "$work/bootloader.2.bin"
cmp "$work/bootloader.1.bin" "$work/bootloader.2.bin"
```

Prepare the serial-specific config and offline reference:

```sh
tools/stlink-v2-bootloader/prepare.py \
  "$work/bootloader.1.bin" "$serial" "$work"
make -C tools/stlink-v2-bootloader \
  LIBOPENCM3=/path/to/libopencm3 BUILD="$PWD/$work" CONFIG_DIR="$PWD/$work" \
  "$PWD/$work/bootpatch.bin"
```

Cold reconnect into the loader, upload `bootpatch.bin` through the same
exact-topology wrapper, and run read-only preflight:

```sh
STLINK_TOOL=/path/to/stlink-tool \
  scripts/stlink-v2-clone-serial.zsh load "$topology" "$work/bootpatch.bin"
tools/stlink-v2-bootloader/invoke_patch.py \
  --topology "$topology" --manifest "$work/manifest.json"
```

Review every reported value. Only then enable the write:

```sh
tools/stlink-v2-bootloader/invoke_patch.py \
  --topology "$topology" --manifest "$work/manifest.json" --write
```

Cold reconnect and verify the loader serial. Restore the matching 12-character
V2J48S7 application prepared by `scripts/stlink-v2-clone-serial.zsh`, cold
reconnect again, and complete the checks in
`docs/STLINK_CLONE_REPAIR.md`.

The preflight is idempotent. Re-running it on a repaired loader verifies the
patched CRC and both patched anchors, reports `already patched and verified`,
and performs no flash write.

Captured and patched ST binaries are generated under ignored `build/` paths
and must never be committed or redistributed.
