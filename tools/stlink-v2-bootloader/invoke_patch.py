#!/usr/bin/env python3
import argparse
import json
import struct
import sys
from pathlib import Path

import usb.core
import usb.util

VID = 0x0483
PID = 0x3748
SUCCESS = 0x5A
ALREADY_PATCHED = 0x5B
REPLY = struct.Struct("<8s8I")

STATUS = {
    0: "preflight passed",
    1: "bad command",
    2: "unexpected original bootloader CRC",
    3: "serial descriptor anchor mismatch",
    4: "serial formatter anchor mismatch",
    5: "flash controller would not unlock",
    6: "descriptor page erase failed",
    7: "descriptor page program failed",
    8: "descriptor page verify failed",
    9: "formatter page erase failed",
    10: "formatter page program failed",
    11: "formatter page verify failed",
    12: "final bootloader CRC mismatch",
    13: "bootloader flash is write-protected",
    SUCCESS: "patch verified successfully",
    ALREADY_PATCHED: "already patched and verified",
}


def parse_topology(topology):
    bus_text, ports_text = topology.split("-", 1)
    return int(bus_text), tuple(int(part) for part in ports_text.split("."))


def find_exact_device(topology):
    expected_bus, expected_ports = parse_topology(topology)
    matches = [
        d for d in usb.core.find(find_all=True, idVendor=VID, idProduct=PID)
        if d.bus == expected_bus
        and tuple(getattr(d, "port_numbers", ()) or ()) == expected_ports
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one {VID:04x}:{PID:04x} at USB {topology}; found {len(matches)}"
        )
    return matches[0]


def transact(dev, command):
    written = dev.write(0x02, command, timeout=2000)
    if written != len(command):
        raise RuntimeError(f"short USB write: {written}/{len(command)}")
    data = bytes(dev.read(0x81, REPLY.size, timeout=10000))
    if len(data) != REPLY.size:
        raise RuntimeError(f"short USB reply: {len(data)}/{REPLY.size}")
    values = REPLY.unpack(data)
    if values[0] != b"FDSPATCH":
        raise RuntimeError(f"bad reply magic: {values[0]!r}")
    keys = (
        "status", "before_crc", "after_crc", "flash_sr",
        "expected_after_crc", "flash_wrpr", "device_id", "flash_kib",
    )
    return dict(zip(keys, values[1:]))


def show(label, result):
    print(label)
    status = result.get("status")
    print(f"  status:      {status} ({STATUS.get(status, 'unknown')})")
    print(f"  before CRC:  {result.get('before_crc'):08x}")
    print(f"  after CRC:   {result.get('after_crc'):08x}")
    print(f"  expected:    {result.get('expected_after_crc'):08x}")
    print(f"  FLASH_SR:    {result.get('flash_sr'):08x}")
    print(f"  FLASH_WRPR:  {result.get('flash_wrpr'):08x}")
    print(f"  device ID:   {result.get('device_id'):08x}")
    print(f"  flash size:  {result.get('flash_kib')} KiB")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--topology", required=True, help="physical USB path, for example 1-8.2")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--write", action="store_true", help="apply the guarded patch")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text())
    original_crc = int(manifest["original_crc32"], 16)
    patched_crc = int(manifest["patched_crc32"], 16)
    serial = manifest["serial"]
    if len(serial) != 12 or any(c not in "0123456789ABCDEF" for c in serial):
        raise RuntimeError("manifest has an invalid serial")

    dev = find_exact_device(args.topology)
    print(f"device: bus {dev.bus}, address {dev.address}, ports {tuple(dev.port_numbers)}")
    dev.set_configuration()
    usb.util.claim_interface(dev, 0)
    try:
        preflight = transact(dev, b"PREFLIGHT:F1A5!!")
        show("preflight:", preflight)
        already_patched = (
            preflight["status"] == ALREADY_PATCHED
            and preflight["before_crc"] == patched_crc
            and preflight["after_crc"] == patched_crc
            and preflight["expected_after_crc"] == patched_crc
            and preflight["flash_wrpr"] == 0xFFFFFFFF
            and preflight["flash_kib"] in (64, 128)
        )
        safe = (
            preflight["status"] == 0
            and preflight["before_crc"] == original_crc
            and preflight["expected_after_crc"] == patched_crc
            and preflight["flash_wrpr"] == 0xFFFFFFFF
            and preflight["flash_kib"] in (64, 128)
        )
        if not (safe or already_patched):
            raise RuntimeError("preflight did not meet every write-safety condition")
        if already_patched:
            print(f"{serial} is already present; no flash write is needed")
            return 0
        if not args.write:
            print("preflight only; no flash was modified")
            return 0

        result = transact(dev, b"PATCHBOOT:F1A5!!")
        show("write result:", result)
        if result["status"] != SUCCESS or result["after_crc"] != patched_crc:
            raise RuntimeError("on-device patch did not verify successfully")
        print(f"cold-unplug the probe now; the next enumeration should use {serial}")
        return 0
    finally:
        usb.util.release_interface(dev, 0)
        usb.util.dispose_resources(dev)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
