#!/usr/bin/env python3
"""Live QTest contract for optional X4 Pro BM8563 RTC + CW2017 virtual I2C.

This is simulated board-level MMIO, NOT evidence of real RTC/battery telemetry,
GT911 functionality, SD mount, e-paper output or successful firmware boot.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import shutil

I2C = 0x60013000
RTC = 0x51
GAUGE = 0x63

class Protocol:
    def __init__(self):
        self.commands: list[str] = []
        self.read_names: list[str] = []
    def write(self, off: int, value: int) -> None:
        self.commands.append(f"writel 0x{I2C+off:x} 0x{value:x}")
    def read(self, off: int, name: str) -> None:
        self.commands.append(f"readl 0x{I2C+off:x}")
        self.read_names.append(name)
    def reset(self):
        self.write(0x18, 0x3000)
        self.write(0x24, 0xffffffff)
        self.write(0x04, 0x10)
        self.write(0x58, 0x3000)
    def send(self, address: int, payload: list[int], name: str):
        # 32-byte FIFO; 1 address + up to 31 payload bytes.
        assert len(payload) <= 31
        self.reset()
        for value in [address << 1, *payload]:
            self.write(0x1c, value)
        self.write(0x5c, 0x900 | (len(payload) + 1))
        self.write(0x60, 0x1000)
        self.write(0x04, 0x30)
        self.read(0x20, name + ".write_status")
    def recv(self, address: int, register: int, name: str):
        self.send(address, [register], name + ".pointer")
        self.reset()
        self.write(0x1c, (address << 1) | 1)
        self.write(0x5c, 0x901)
        self.write(0x60, 0x1801)
        self.write(0x64, 0x1000)
        self.write(0x04, 0x30)
        self.read(0x20, name + ".read_status")
        self.read(0x1c, name + ".value")
    def text(self):
        return "\n".join(self.commands) + "\n"

def make_protocol(enabled: bool) -> Protocol:
    p = Protocol()
    p.recv(RTC, 0x00, "rtc.control_initial")
    if not enabled:
        p.recv(GAUGE, 0x00, "gauge.version")
        return p
    p.send(RTC, [0x00, 0x01], "rtc.control_write")
    p.recv(RTC, 0x00, "rtc.control_readback")
    p.recv(RTC, 0x02, "rtc.seconds")
    p.recv(GAUGE, 0x00, "gauge.version")
    p.recv(GAUGE, 0x04, "gauge.soc_before_profile")
    p.recv(GAUGE, 0x0b, "gauge.flag_before_profile")
    # Eight full I2C writes cover all 80 profile bytes, in 10-byte chunks.
    for start in range(0, 80, 10):
        p.send(GAUGE, [0x10 + start, *range(start, start + 10)],
               f"gauge.profile_chunk_{start}")
    p.send(GAUGE, [0x0b, 0x80], "gauge.commit_profile")
    p.recv(GAUGE, 0x0b, "gauge.flag_after_profile")
    p.recv(GAUGE, 0x04, "gauge.soc_after_profile")
    p.recv(GAUGE, 0x10, "gauge.profile_start")
    p.recv(GAUGE, 0x5f, "gauge.profile_end")
    p.recv(GAUGE, 0x02, "gauge.vcell_high")
    p.recv(GAUGE, 0x03, "gauge.vcell_low")
    return p

def decode(p: Protocol, stdout: str, enabled: bool) -> dict:
    if "FAIL " in stdout:
        raise AssertionError("QTest command error: " + stdout[-1000:])
    results = [int(value, 16) for value in re.findall(
        r"(?m)^OK\s+0x([a-fA-F0-9]+)\s*$", stdout)]
    if len(results) != len(p.read_names):
        raise AssertionError(
            f"Read count mismatch: expected {len(p.read_names)}, received "
            f"{len(results)}; stdout-tail: {stdout[-800:]}")
    measured = dict(zip(p.read_names, results))
    expected_status = 0x80 if enabled else 0x400
    for name,value in measured.items():
        if name.endswith((".write_status", ".read_status")):
            if value != expected_status:
                raise AssertionError(f"Bad {name}: {value:#x}, expected {expected_status:#x}")
    if enabled:
        assert measured["rtc.control_initial.value"] == 0
        assert measured["rtc.control_readback.value"] == 1
        second = measured["rtc.seconds.value"]
        assert (second & 0x7f) <= 0x59 and second & 0x0f <= 9
        assert measured["gauge.version.value"] == 0x0d
        assert measured["gauge.soc_before_profile.value"] == 0
        assert measured["gauge.flag_before_profile.value"] & 0x80 == 0
        assert measured["gauge.flag_after_profile.value"] & 0x80 == 0x80
        assert measured["gauge.soc_after_profile.value"] == 70
        assert measured["gauge.profile_start.value"] == 0
        assert measured["gauge.profile_end.value"] == 79
        raw = (measured["gauge.vcell_high.value"] << 8) | measured["gauge.vcell_low.value"]
        mv = (raw * 5 + 8) >> 4
        assert abs(mv - 3800) <= 2, mv
    else:
        assert measured["rtc.control_initial.value"] == 0xee
        assert measured["gauge.version.value"] == 0xee
    return {"profile_enabled": enabled, "read_count": len(measured),
            "no_device_nack_when_disabled": not enabled,
            "rtc_register_readback": enabled,
            "rtc_bcd_clock": enabled,
            "cw2017_profile_80_bytes": enabled,
            "cw2017_soc_requires_profile": enabled,
            "simulated_soc_percent": 70 if enabled else None,
            "simulated_voltage_mv": 3800 if enabled else None,
            "touch_controller_emulated": False,
            "guest_application_boot_verified": False}

def run_qemu(args, enabled: bool) -> dict:
    qemu = (args.qemu_root / "build-ybx/qemu-system-xtensa").resolve(strict=True)
    p = make_protocol(enabled)
    with TemporaryDirectory(prefix="ybx-x4pro-i2c-") as tmp:
        shutil.copyfile(args.rom, Path(tmp) / "esp32s3_rev0_rom.bin")
        machine = "esp32s3,ybx-x4pro-i2c=on" if enabled else "esp32s3"
        cmd = [
            "docker", "run", "--rm", "-i", "--network", "none",
            "--platform", "linux/arm64",
            "-v", f"{qemu.parent.parent}:/qemu:ro",
            "-v", f"{tmp}:/bios:ro",
            args.docker_image, "timeout", "-k", "2s", "7s",
            "/qemu/build-ybx/qemu-system-xtensa",
            "-machine", machine, "-nographic", "-monitor", "none",
            "-serial", "null", "-qtest", "stdio", "-L", "/bios",
        ]
        result = subprocess.run(cmd, input=p.text(), capture_output=True,
                                text=True, timeout=25)
        if result.returncode not in (0,124):
            raise RuntimeError(
                f"QEMU exit={result.returncode}: {result.stderr[-1500:]}")
        evidence = decode(p, result.stdout, enabled)
    evidence["qemu_sha256"] = hashlib.sha256(qemu.read_bytes()).hexdigest()
    evidence["rom_sha256"] = hashlib.sha256(args.rom.read_bytes()).hexdigest()
    evidence["qtest_only"] = True
    return evidence

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu-root", required=True, type=Path)
    parser.add_argument("--rom", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--docker-image", default="ybx-qemu-builder:20261029")
    args = parser.parse_args()
    off = run_qemu(args, False)
    on = run_qemu(args, True)
    report = {"disabled": off, "enabled": on}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()
