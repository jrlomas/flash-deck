#!/usr/bin/env python3
"""Capture the protected 16 KiB loader through the temporary dumper app."""

import argparse
import struct
import sys
import zlib
from pathlib import Path

import usb.core
import usb.util

VID = 0x0483
PID = 0x3748
TOTAL = 16 + 0x4000


def parse_topology(value):
    try:
        bus_text, ports_text = value.split("-", 1)
        return int(bus_text), tuple(int(part) for part in ports_text.split("."))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("topology must look like 1-8.2") from exc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--topology", required=True, type=parse_topology)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    expected_bus, expected_ports = args.topology
    matches = [
        dev for dev in usb.core.find(find_all=True, idVendor=VID, idProduct=PID)
        if dev.bus == expected_bus
        and tuple(getattr(dev, "port_numbers", ()) or ()) == expected_ports
    ]
    if len(matches) != 1:
        raise SystemExit(f"expected one probe at {expected_bus}-{'.'.join(map(str, expected_ports))}; found {len(matches)}")

    dev = matches[0]
    dev.set_configuration()
    usb.util.claim_interface(dev, 0)
    try:
        if dev.write(0x02, bytes([ord("D")]) + bytes(15), timeout=2000) != 16:
            raise RuntimeError("short dump command")
        stream = bytearray()
        while len(stream) < TOTAL:
            stream.extend(dev.read(0x81, min(1024, TOTAL - len(stream)), timeout=3000))
    finally:
        usb.util.release_interface(dev, 0)
        usb.util.dispose_resources(dev)

    magic, length, expected_crc = struct.unpack_from("<8sII", stream)
    data = bytes(stream[16:])
    actual_crc = zlib.crc32(data)
    if magic != b"STL2DUMP" or length != 0x4000 or actual_crc != expected_crc:
        raise SystemExit("invalid bootloader dump header or CRC")
    args.output.write_bytes(data)
    print(f"captured {len(data)} bytes, CRC32 {actual_crc:08x}: {args.output}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
