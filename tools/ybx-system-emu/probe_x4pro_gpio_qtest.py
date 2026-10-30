#!/usr/bin/env python3
"""QTest GPIO output/enable registers for opt-in ESP32-S3 X4 Pro board.

Tests the GPIO MMIO side needed for later GT911 power/reset/INT coupling;
does NOT test touch controller, pull resistors, interrupts or device boot.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import argparse
import hashlib
import json
import re
import shutil
import subprocess

GPIO = 0x60004000

def protocol():
    commands = []
    def wr(offset, value):
        commands.append(f"writel 0x{GPIO+offset:x} 0x{value:x}")
    def rd(offset):
        commands.append(f"readl 0x{GPIO+offset:x}")
    rd(0x38)             # boot strap unaffected
    rd(0x20)             # enable low initial
    wr(0x24, 0x414)      # enable GPIO2 (power), GPIO4 (reset), GPIO10 (INT)
    rd(0x20)
    wr(0x08, 0x404)      # GPIO2=1 (power off), GPIO10=1
    rd(0x04)
    wr(0x0c, 0x004)      # GPIO2=0 (power on)
    rd(0x04)
    rd(0x3c)            # enabled pin input readback
    wr(0x28, 0x400)      # disable GPIO10
    rd(0x20)
    wr(0x14, 0x002)      # output GPIO33 high, high bank
    rd(0x10)
    wr(0x30, 0x002)      # enable GPIO33
    rd(0x2c)
    rd(0x40)
    return "\n".join(commands) + "\n"

def validate(stdout: str, enabled: bool):
    reads = [int(s,16) for s in re.findall(
        r"(?m)^OK\s+0x([0-9a-fA-F]+)\s*$",stdout)]
    expected = ([4, 0, 0x414, 0x404, 0x400, 0x400,
                 0x14, 0x2, 0x2, 0x2] if enabled else
                [4, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    if "FAIL " in stdout or reads != expected:
        raise RuntimeError(
            f"GPIO {enabled=} unexpected: expected {expected}, got {reads}, "
            f"output={stdout[-350:]}")
    return {"gpio_output_mmio_enabled":enabled, "gpio2_active_low_level_written":enabled,
            "gpio4_gpio10_direction_writes":enabled,
            "gpio33_high_bank_readback":enabled, "strap_preserved":True,
            "gt911_power_gate_verified":False,
            "guest_firmware_boot_verified":False}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu-root",type=Path,required=True)
    parser.add_argument("--rom",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--docker-image",default="ybx-qemu-builder:20261029")
    a=parser.parse_args()
    qemu=(a.qemu_root/"build-ybx/qemu-system-xtensa").resolve(strict=True)
    results={}
    with TemporaryDirectory(prefix="ybx-s3-gpio-qtest-") as temp:
        shutil.copyfile(a.rom,Path(temp)/"esp32s3_rev0_rom.bin")
        for enabled in (False,True):
            cmd=[
                "docker","run","--rm","-i","--network","none",
                "--platform","linux/arm64",
                "-v",f"{qemu.parent.parent}:/qemu:ro",
                "-v",f"{temp}:/bios:ro",
                a.docker_image,"timeout","-k","2s","7s",
                "/qemu/build-ybx/qemu-system-xtensa",
                "-machine","esp32s3,ybx-x4pro-i2c=on" if enabled else "esp32s3",
                "-nographic","-monitor","none","-serial","null",
                "-qtest","stdio","-L","/bios"]
            result=subprocess.run(cmd,input=protocol(),capture_output=True,
                                  text=True,timeout=25)
            if result.returncode not in (0,124):
                raise RuntimeError(f"QEMU {enabled=} exit {result.returncode}: {result.stderr[-700:]}")
            results["enabled" if enabled else "disabled"]=validate(result.stdout,enabled)
    results["qemu_sha256"]=hashlib.sha256(qemu.read_bytes()).hexdigest()
    results["rom_sha256"]=hashlib.sha256(a.rom.read_bytes()).hexdigest()
    results["qtest_only"]=True
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(results,indent=2)+"\n")
    print(json.dumps(results,indent=2))

if __name__=="__main__":
    main()
