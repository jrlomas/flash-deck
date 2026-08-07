#!/usr/bin/env python3
"""Validate a known clone loader and prepare a serial-specific patch build."""

import argparse
import hashlib
import json
import struct
import zlib
from pathlib import Path

KNOWN_IMAGES = {
    "510909fd7f2a85a0b3f9fed6c41a6dfc4bb9925f6b084fd94ef8715958334481":
        "loader metadata 0x4d / trailer 0x072c",
    "61a6dc5aa3a5cb68b7fe09c8a93eec8d49b732df7352ba3fc429c916bea57e38":
        "loader metadata 0x4f / trailer 0x4729",
    "04d08466c2b452c2e6065103ed9dc91ef3370628ad0bd6539502dbad22e71c32":
        "loader metadata 0x4f / trailer 0x072c",
}
KNOWN_SIZE = 0x4000
FORMATTER_OFFSET = 0x11E0
DESCRIPTOR_OFFSET = 0x2A40
FORMATTER_ANCHOR = bytes.fromhex("10b49348016842688068d9b1")
FORMATTER_PATCH = bytes.fromhex("704700bf")
ORIGINAL_DESCRIPTOR = bytes([0x1A, 0x03]) + "000000000001".encode("utf-16le")


def validate_serial(value):
    serial = value.upper()
    if len(serial) != 12 or any(c not in "0123456789ABCDEF" for c in serial):
        raise argparse.ArgumentTypeError("serial must be exactly 12 hexadecimal characters")
    return serial


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("bootloader", type=Path)
    parser.add_argument("serial", type=validate_serial)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    original = args.bootloader.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if len(original) != KNOWN_SIZE:
        raise SystemExit(f"refusing bootloader size {len(original)}; expected {KNOWN_SIZE}")
    if digest not in KNOWN_IMAGES:
        raise SystemExit(f"refusing unknown bootloader SHA-256 {digest}")
    if struct.unpack_from("<II", original) != (0x20000800, 0x08002765):
        raise SystemExit("refusing unexpected bootloader vector table")
    if original[FORMATTER_OFFSET:FORMATTER_OFFSET + len(FORMATTER_ANCHOR)] != FORMATTER_ANCHOR:
        raise SystemExit("refusing formatter anchor mismatch")
    if original[DESCRIPTOR_OFFSET:DESCRIPTOR_OFFSET + len(ORIGINAL_DESCRIPTOR)] != ORIGINAL_DESCRIPTOR:
        raise SystemExit("refusing serial descriptor anchor mismatch")

    descriptor = bytes([0x1A, 0x03]) + args.serial.encode("utf-16le")
    patched = bytearray(original)
    patched[DESCRIPTOR_OFFSET:DESCRIPTOR_OFFSET + len(descriptor)] = descriptor
    patched[FORMATTER_OFFSET:FORMATTER_OFFSET + len(FORMATTER_PATCH)] = FORMATTER_PATCH
    patched = bytes(patched)

    changed = [
        index for index, (before, after) in enumerate(zip(original, patched))
        if before != after
    ]
    if not changed or any(
        not (
            FORMATTER_OFFSET <= index < FORMATTER_OFFSET + len(FORMATTER_PATCH)
            or DESCRIPTOR_OFFSET + 2 <= index < DESCRIPTOR_OFFSET + len(descriptor)
        )
        for index in changed
    ):
        raise SystemExit("internal error: patch changed bytes outside approved ranges")

    original_crc = zlib.crc32(original)
    patched_crc = zlib.crc32(patched)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "expected-patched-bootloader.bin").write_bytes(patched)

    descriptor_values = ", ".join(f"0x{byte:02x}" for byte in descriptor)
    config = (
        "#ifndef BOOTPATCH_CONFIG_H\n"
        "#define BOOTPATCH_CONFIG_H\n"
        f"#define ORIGINAL_CRC 0x{original_crc:08x}U\n"
        f"#define PATCHED_CRC 0x{patched_crc:08x}U\n"
        f"#define PATCHED_DESCRIPTOR_BYTES {descriptor_values}\n"
        "#endif\n"
    )
    (args.output / "bootpatch_config.h").write_text(config)

    manifest = {
        "serial": args.serial,
        "source_sha256": digest,
        "source_variant": KNOWN_IMAGES[digest],
        "original_crc32": f"{original_crc:08x}",
        "patched_crc32": f"{patched_crc:08x}",
        "patched_sha256": hashlib.sha256(patched).hexdigest(),
        "changed_offsets": [f"0x{index:04x}" for index in changed],
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
