#!/usr/bin/env python3
"""Live QTest: X4 Pro native SD power uses GPIO1 HIGH and GPIO5 LOW.

Uses real QEMU SD-card state/driver, no fabricated SD responses.
Does not prove the ESP-IDF guest mounted FAT or rendered a page.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

SD=0x60028000
GPIO=0x60004000

def scenario(with_gate=True):
    names=[]
    commands=[]
    def wr(addr, value):
        commands.append(f"writel 0x{addr:x} 0x{value:x}")
    def rd(addr, name):
        names.append(name)
        commands.append(f"readl 0x{addr:x}")
    def cmd8(label):
        wr(SD+0x44,0xffffffff)
        wr(SD+0x28,0x1aa)
        wr(SD+0x2c,0x80000148)
        rd(SD+0x44,label+".irq")
        rd(SD+0x30,label+".r0")
    def idle():
        wr(SD+0x44,0xffffffff)
        wr(SD+0x28,0)
        wr(SD+0x2c,0x80000000)
    wr(SD+0x00,0x7)
    wr(SD+0x00,0x10)
    idle()
    cmd8("initial")
    if with_gate:
        # Inputs default high-impedance. GPIO enable first with
        # GPIO1 LOW (master OFF) and GPIO5 LOW (slot ON).
        wr(GPIO+0x24, (1<<1)|(1<<5))
        idle()
        cmd8("master_low")
        wr(GPIO+0x08,1<<1) # master ON, slot already LOW
        idle()
        cmd8("both_rails_on")
        wr(GPIO+0x08,1<<5) # GPIO5 HIGH -> slot disabled
        idle()
        cmd8("slot_off")
        wr(GPIO+0x0c,1<<5)
        idle()
        cmd8("slot_reenabled")
        wr(GPIO+0x0c,1<<1) # master OFF, slot ON
        idle()
        cmd8("master_off")
        wr(GPIO+0x08,1<<1) # master ON, slot ON -> reset card
        idle()
        cmd8("master_reenabled")
    return "\n".join(commands)+"\n",names

def parse(stdout, names):
    if "FAIL " in stdout:
        raise AssertionError(stdout[-500:])
    values=[int(x,16) for x in re.findall(
        r"(?m)^OK\s+0x([0-9a-fA-F]+)\s*$",stdout)]
    if len(values)!=len(names):
        raise AssertionError(f"qtest readbacks expected {len(names)}, got {len(values)}")
    return dict(zip(names,values))

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--qemu-root",type=Path,required=True)
    ap.add_argument("--rom",type=Path,required=True)
    ap.add_argument("--sd-image",type=Path,required=True)
    ap.add_argument("--out",type=Path,required=True)
    ap.add_argument("--docker-image",default="ybx-qemu-builder:20261029")
    a=ap.parse_args()
    qemu=(a.qemu_root/"build-ybx/qemu-system-xtensa").resolve(strict=True)
    results={}
    with TemporaryDirectory(prefix="ybx-sd-power-") as td:
        root=Path(td)
        (root/"rom").mkdir()
        shutil.copyfile(a.rom,root/"rom/esp32s3_rev0_rom.bin")
        card=root/"card.img"
        shutil.copyfile(a.sd_image,card)
        for gate in (False,True):
            script,names=scenario(gate)
            machine="esp32s3,ybx-x4pro-sd-power=on" if gate else "esp32s3"
            cmd=[
                "docker","run","--rm","-i","--network","none",
                "--platform","linux/arm64",
                "-v",f"{qemu.parent.parent}:/qemu:ro",
                "-v",f"{root}:/work",
                a.docker_image,"timeout","-k","2s","7s",
                "/qemu/build-ybx/qemu-system-xtensa",
                "-machine",machine,"-accel","qtest",
                "-qtest","stdio","-nographic","-monitor","none",
                "-serial","null","-L","/work/rom",
                "-drive","file=/work/card.img,if=sd,format=raw",
            ]
            run=subprocess.run(cmd,input=script,text=True,
                               capture_output=True,timeout=25)
            if run.returncode not in (0,124):
                raise RuntimeError(f"QEMU {machine} returned {run.returncode}: {run.stderr[-900:]}")
            readings=parse(run.stdout,names)
            results["gated" if gate else "default"]={
                "machine":machine,"readings":{
                    k:hex(v) for k,v in readings.items()
                },"read_count":len(readings)}
            if gate:
                for case in ("initial","master_low","slot_off","master_off"):
                    assert readings[case+".irq"] & 0x100, (case, readings)
                for case in ("both_rails_on","slot_reenabled","master_reenabled"):
                    assert readings[case+".irq"] & 0x100 == 0, (case,readings)
                    assert readings[case+".irq"] & 0x4, (case,readings)
                    assert readings[case+".r0"] == 0x1aa,(case,readings)
            else:
                assert readings["initial.r0"] == 0x1aa,readings
                assert readings["initial.irq"] & 0x100 == 0,readings
    report={
        "qemu_sha256":hashlib.sha256(qemu.read_bytes()).hexdigest(),
        "rom_sha256":hashlib.sha256(a.rom.read_bytes()).hexdigest(),
        "sd_image_sha256":hashlib.sha256(a.sd_image.read_bytes()).hexdigest(),
        "real_card_power_down_nack":True,
        "gpio1_master_high_required":True,
        "gpio5_active_low_required":True,
        "re_power_resets_sd_protocol":True,
        "ungated_default_machine_preserved":True,
        "fat_mount_by_guest_verified":False,
        "real_hardware_verified":False,
        "results":results,
    }
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report,indent=2))

if __name__=="__main__":
    main()
