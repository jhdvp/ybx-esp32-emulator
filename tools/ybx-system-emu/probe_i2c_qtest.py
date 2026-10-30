#!/usr/bin/env python3
"""QTest MMIO contract for ESP32-C3/S3 I2C master without fake I2C slaves.

Tests a real compiled QEMU binary using an empty bus, independent of app boot.
USB, touch, or a successful YBX firmware boot are NOT tested here.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import argparse
import hashlib
import json
import re
import shutil
import subprocess

BIOS = {"c3": "esp32c3-rom.bin", "s3": "esp32s3_rev0_rom.bin"}
QEMU_TARGET = {"c3": "riscv32", "s3": "xtensa"}
ADDR = 0x60013000

def protocol() -> tuple[str, list[int]]:
    # ESP-IDF5 WRITE = 1, STOP = 2, RESTART = 6 (see i2c_ll.h).
    commands = [
        f"writel 0x{ADDR+0x18:x} 0x3000", # clear both FIFOs
        f"writel 0x{ADDR+0x04:x} 0x10",   # master mode
        f"writel 0x{ADDR+0x1c:x} 0xba",   # 0x5D (absent) + write
        f"writel 0x{ADDR+0x58:x} 0x901",  # write one byte + ACK check
        f"writel 0x{ADDR+0x5c:x} 0x1000", # modern STOP opcode
        f"writel 0x{ADDR+0x28:x} 0x400",  # enable NACK interrupt
        f"writel 0x{ADDR+0x04:x} 0x30",   # trigger transfer
        f"readl 0x{ADDR+0x20:x}",          # raw NACK must be 0x400
        f"readl 0x{ADDR+0x2c:x}",          # enabled interrupt status
        f"readl 0x{ADDR+0x58:x}",          # command DONE (31)
        f"writel 0x{ADDR+0x24:x} 0x400",  # clear NACK
        f"readl 0x{ADDR+0x20:x}",          # no remaining NACK
        f"writel 0x{ADDR+0x58:x} 0x3000",  # C3/S3 RESTART opcode 6
        f"writel 0x{ADDR+0x5c:x} 0x1000",  # C3/S3 STOP opcode 2
        f"writel 0x{ADDR+0x04:x} 0x30",    # execute empty transaction
        f"readl 0x{ADDR+0x58:x}",          # modern RESTART done
        f"readl 0x{ADDR+0x5c:x}",          # modern STOP done
        f"readl 0x{ADDR+0x20:x}",          # TRANS_COMPLETE bit 7
    ]
    return "\n".join(commands) + "\n", [
        0x400, 0x400, 0x80000901, 0, 0x80003000, 0x80001000, 0x80
    ]

def validate_responses(stdout: str) -> dict:
    # Writes return "OK", reads return "OK 0x...".
    reads = [int(x, 16) for x in re.findall(
        r"(?m)^OK\s+0x([0-9a-fA-F]+)\s*$", stdout)]
    _, expected = protocol()
    if reads != expected:
        raise RuntimeError(f"I2C QTest mismatch: received {reads!r}, expected {expected!r}")
    return {"nack_raw": reads[0], "nack_irq": reads[1],
            "first_command_done": bool(reads[2] & 0x80000000),
            "nack_cleared": reads[3] == 0,
            "modern_restart_done": bool(reads[4] & 0x80000000),
            "modern_stop_done": bool(reads[5] & 0x80000000),
            "stop_interrupt": reads[6] == 0x80,
            "no_fake_i2c_device": True}

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chip", choices=tuple(BIOS), required=True)
    parser.add_argument("--qemu-root", type=Path, required=True)
    parser.add_argument("--rom", type=Path, required=True)
    parser.add_argument("--docker-image", default="ybx-qemu-builder:20261029")
    parser.add_argument("--out", type=Path, help="write reproducible QTest report JSON")
    args = parser.parse_args()
    qemu = (args.qemu_root / "build-ybx" /
            f"qemu-system-{QEMU_TARGET[args.chip]}").resolve(strict=True)
    with TemporaryDirectory(prefix="ybx-i2c-qtest-") as tmp:
        bios = Path(tmp) / BIOS[args.chip]
        shutil.copyfile(args.rom, bios)
        args_to_qemu = [
            "docker", "run", "--rm", "-i", "--network", "none",
            "--platform", "linux/arm64",
            "--mount", f"type=bind,src={qemu.parent.parent},dst=/qemu,readonly",
            "--mount", f"type=bind,src={tmp},dst=/bios,readonly",
            args.docker_image, "timeout", "-k", "2s", "7s",
            f"/qemu/build-ybx/qemu-system-{QEMU_TARGET[args.chip]}",
            "-machine", "esp32" + args.chip, "-nographic",
            "-monitor", "none", "-serial", "null",
            "-qtest", "stdio", "-L", "/bios",
        ]
        result = subprocess.run(args_to_qemu, input=protocol()[0],
                                capture_output=True, text=True, timeout=25)
        # QTest stdio has no `quit` command; a bounded host kill ends
        # the session after all MMIO replies have been flushed.
        if result.returncode not in (0, 124) or "FAIL " in result.stdout:
            raise RuntimeError(
                f"QEMU exit={result.returncode}: {result.stderr[-1600:]} "
                f"stdout={result.stdout[-1000:]}")
        evidence = validate_responses(result.stdout)
        evidence.update({
            "chip": args.chip,
            "qemu_sha256": hashlib.sha256(qemu.read_bytes()).hexdigest(),
            "rom_sha256": hashlib.sha256(args.rom.read_bytes()).hexdigest(),
            "qtest_only": True, "guest_application_boot_verified": False,
        })
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(evidence, indent=2) + "\n")
        print(f"{args.chip}: {evidence}")

if __name__ == "__main__":
    main()
