#!/usr/bin/env python3
"""Conservative MMIO QTest for experimental ESP32-C3 ADC one-shot handshake."""
from pathlib import Path
from tempfile import TemporaryDirectory
import argparse
import hashlib
import json
import re
import shutil
import subprocess

ADC = 0x60040000
DONE_1 = 0x80000000


def protocol():
    commands = [
        f"writel 0x{ADC+0x20:x} 0x80000000",   # ADC1 select, not START
        f"readl 0x{ADC+0x44:x}",               # no false DONE yet
        f"writel 0x{ADC+0x20:x} 0xa0000000",   # ADC1+START
        f"readl 0x{ADC+0x44:x}",               # DONE bit31
        f"readl 0x{ADC+0x2c:x}",               # zero dummy sample
        f"writel 0x{ADC+0x4c:x} 0x80000000",   # acknowledge DONE
        f"readl 0x{ADC+0x44:x}",               # DONE cleared
    ]
    return "\n".join(commands)+"\n", [0, DONE_1, 0, 0]


def validate(stdout: str):
    received = [int(x,16) for x in re.findall(r"(?m)^OK\s+0x([0-9a-fA-F]+)\s*$",stdout)]
    _, expected = protocol()
    if "FAIL " in stdout or received != expected:
        raise RuntimeError(f"C3 ADC QTest mismatch: expected {expected}, got {received}, output={stdout[-250:]}")
    return {
        "no_spurious_done": True, "adc1_start_done": True,
        "dummy_sample_is_zero": True, "done_cleared": True,
        "genuine_sensor_voltage_verified": False,
        "real_firmware_ready_verified": False,
    }


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu-root",type=Path,required=True)
    parser.add_argument("--rom",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--docker-image",default="ybx-qemu-builder:20261029")
    a=parser.parse_args()
    qemu=(a.qemu_root/"build-ybx/qemu-system-riscv32").resolve(strict=True)
    with TemporaryDirectory(prefix="ybx-c3-adc-qtest-") as tmp:
        shutil.copyfile(a.rom,Path(tmp)/"esp32c3-rom.bin")
        cmd=[
            "docker","run","--rm","-i","--network","none",
            "--platform","linux/arm64",
            "--mount",f"type=bind,src={qemu.parent.parent},dst=/qemu,readonly",
            "--mount",f"type=bind,src={tmp},dst=/bios,readonly",
            a.docker_image,"timeout","-k","2s","7s",
            "/qemu/build-ybx/qemu-system-riscv32",
            "-machine","esp32c3","-nographic","-monitor","none",
            "-serial","null","-qtest","stdio","-L","/bios",
        ]
        result=subprocess.run(cmd,input=protocol()[0],capture_output=True,text=True,timeout=25)
        if result.returncode not in (0,124):
            raise RuntimeError(f"QEMU exit {result.returncode}: {result.stderr[-600:]}")
        evidence=validate(result.stdout)
        evidence.update({
            "qemu_sha256":hashlib.sha256(qemu.read_bytes()).hexdigest(),
            "rom_sha256":hashlib.sha256(a.rom.read_bytes()).hexdigest(),
            "qtest_only":True,
        })
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(evidence,indent=2)+"\n")
    print(json.dumps(evidence))

if __name__=="__main__":
    main()
