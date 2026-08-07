# ST-LINK/V2 clone single-serial rollout

## Required end state

Every clone must expose one persistent 12-character USB serial in both the
protected loader and normal ST-LINK application. A repair is complete only
after all of these checks pass:

1. A cold loader enumeration reports the assigned ASCII serial.
2. The restored V2J48S7 application reports that same ASCII serial.
3. A second cold reconnect starts V2J48S7 with the same serial.
4. CubeProgrammer lists the probe using the ASCII bytes encoded as hexadecimal.
5. The exact topology and final identity are recorded below.

The application-only formatter bypass used during the intermediate experiment
does not satisfy this checklist because the protected loader retains a
different serial.

## Probe queue

| Assigned serial | State | Notes |
| --- | --- | --- |
| `F1A5DEC00001` | Pending | Older clone; current loader and application state not yet inventoried. |
| `F1A5DEC00002` | Complete | Former intermediate application-only probe. Two matching loader captures have SHA-256 `61a6dc5aa3a5cb68b7fe09c8a93eec8d49b732df7352ba3fc429c916bea57e38` and CRC32 `58b520d7`; patched CRC32 `a544aaea` verified on-device. This audited variant differs from probe 00005 only at three tail metadata/checksum bytes. Cold device 40 and V2J48S7 both report `F1A5DEC00002`; CubeProgrammer reports `463141354445433030303032`. |
| `F1A5DEC00003` | Pending | Older clone; previously generated 24-character application artifact is obsolete. |
| `F1A5DEC00004` | Pending | Older clone; previously generated 24-character application artifact is obsolete. |
| `F1A5DEC00005` | Complete | Sacrificial broken-target-connector probe. Bootloader CRC changed from `433f7d29` to verified `b2cab72f`; cold device 28 and V2J48S7 both report `F1A5DEC00005`. CubeProgrammer reports the equivalent `463141354445433030303035`. The repository-built idempotent patcher was subsequently live-tested and returned `already patched and verified` without writing. |

The intermediate-fix probe was identified as `F1A5DEC00002`; it was one of the
original five, not an additional sixth probe. Before each remaining repair,
connect only the candidate clone, record its USB topology and current serial,
then assign the matching pending ID.

## Repeatable process

1. Connect exactly one candidate clone and record its physical USB topology.
2. Prepare the 12-character V2J48S7 application with
   `scripts/stlink-v2-clone-serial.zsh prepare SERIAL`.
3. Enter loader mode, upload the read-only dumper, and capture the protected
   16 KiB bootloader twice.
4. Require byte-identical captures, the known source CRC, and exact formatter
   and descriptor anchors.
5. Build the serial-specific patcher and compare its virtual patched image
   against an offline reference.
6. Upload the patcher into the application slot.
7. Run read-only preflight. Require STM32F1 device ID `0x410`, 64 or 128 KiB
   medium-density flash, `FLASH_WRPR=ffffffff`, and the expected boot CRC.
8. Issue the authenticated write command. Program the descriptor page first
   and formatter page last, verify both pages, and require the final 16 KiB CRC.
9. Cold reconnect and verify the loader reports the assigned serial.
10. Restore the prepared V2J48S7 application with the isolated official
    `program` command, then cold reconnect again. The direct `load` command is
    for temporary dumper/patcher applications; older loaders may not finalize
    ST application metadata when that path is used.
11. Verify sysfs, PyUSB, CubeProgrammer, and stlink-tools all resolve the same
    physical identity. CubeProgrammer represents the ASCII bytes as hex.
12. Record the result in the queue above before moving to another probe.

## Safety invariants

- Isolate the exact USB topology; never select the first matching VID/PID.
- Never commit or redistribute ST firmware or captured bootloader images.
- Keep the two original bootloader captures and their hashes locally.
- Refuse unknown CRCs, non-unique anchors, unexpected flash geometry, or write
  protection.
- Keep a known-good V2J48S7 application image ready for restoration.
