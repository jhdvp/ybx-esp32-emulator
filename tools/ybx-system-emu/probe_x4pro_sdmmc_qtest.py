#!/usr/bin/env python3
"""Live Espressif QEMU DesignWare SDMMC MMIO/controller/card smoke test.

Only verifies command/reset/response behavior in QTest, NOT firmware FAT
mount, SDMMC DMA-sector read/write, or actual card power gating.
"""
from pathlib import Path
import argparse
import hashlib
import json
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory

BASE = 0x60028000  # QEMU include/hw/misc/esp32s3_reg.h
CMD = 0x2c
CMDARG = 0x28
RINTSTS = 0x44
RESP0 = 0x30
INTMASK = 0x24
MINTSTS = 0x40
CTRL = 0x00

def commands():
    lines=[]
    names=[]
    def wr(off,val):lines.append(f"writel 0x{BASE+off:x} 0x{val:x}")
    def rd(off,name):
        lines.append(f"readl 0x{BASE+off:x}")
        names.append(name)
    rd(0x1c,"block_size_initial")
    wr(CTRL,0x7)
    rd(CTRL,"control_after_reset")
    wr(0x1c,512)
    rd(0x1c,"block_size_configured")
    wr(CTRL,0x10) # hardware interrupt enables
    wr(INTMASK,0x4)
    wr(CMDARG,0)
    wr(CMD,0x80000000) # CMD0 go-idle
    rd(CMD,"cmd0_start_cleared")
    rd(RINTSTS,"cmd0_irq_raw")
    rd(MINTSTS,"cmd0_irq_masked")
    wr(RINTSTS,0xffffffff)
    rd(RINTSTS,"irq_cleared")
    wr(CMDARG,0x1aa)
    wr(CMD,0x80000148) # CMD8 send interface condition, resp + CRC
    rd(CMD,"cmd8_start_cleared")
    rd(RINTSTS,"cmd8_irq_raw")
    rd(RESP0,"cmd8_response")
    return "\n".join(lines)+"\n",names

def parse(raw,names):
    if "FAIL " in raw:
        raise AssertionError("QTest failed: "+raw[-800:])
    values=[int(x,16) for x in re.findall(r"(?m)^OK\s+0x([0-9a-fA-F]+)\s*$",raw)]
    if len(values)!=len(names):
        raise AssertionError(f"readback length mismatch {len(values)} != {len(names)}; tail: {raw[-450:]}")
    return dict(zip(names,values))

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--qemu-root",type=Path,required=True)
    ap.add_argument("--rom",type=Path,required=True)
    ap.add_argument("--sd-image",type=Path,required=True)
    ap.add_argument("--out",type=Path,required=True)
    ap.add_argument("--docker-image",default="ybx-qemu-builder:20261029")
    args=ap.parse_args()
    qemu=(args.qemu_root/"build-ybx/qemu-system-xtensa").resolve(strict=True)
    script,read_names=commands()
    with TemporaryDirectory(prefix="ybx-qemu-sdmmc-") as scratch:
        wd=Path(scratch)
        bios=wd/"rom"
        bios.mkdir()
        shutil.copyfile(args.rom,bios/"esp32s3_rev0_rom.bin")
        sd=wd/"sd.img"
        shutil.copyfile(args.sd_image,sd)
        cmd=[
            "docker","run","--rm","-i","--network","none",
            "--platform","linux/arm64",
            "-v",f"{qemu.parent.parent}:/qemu:ro",
            "-v",f"{wd}:/work",
            args.docker_image,"timeout","-k","2s","7s",
            "/qemu/build-ybx/qemu-system-xtensa",
            "-machine","esp32s3","-accel","qtest",
            "-qtest","stdio","-nographic","-monitor","none",
            "-serial","null","-L","/work/rom",
            "-drive","file=/work/sd.img,if=sd,format=raw",
        ]
        run=subprocess.run(cmd,input=script,text=True,
                           capture_output=True,timeout=25)
        if run.returncode not in (0,124):
            raise RuntimeError(f"QEMU returned {run.returncode}: {run.stderr[-1000:]}")
        responses=parse(run.stdout,read_names)
    assert responses["block_size_configured"]==512,responses
    assert responses["control_after_reset"]==0,responses
    assert responses["cmd0_start_cleared"] & 0x80000000==0,responses
    assert responses["cmd0_irq_raw"] & 0x4==0x4,responses
    assert responses["cmd0_irq_masked"] & 0x4==0x4,responses
    assert responses["irq_cleared"]==0,responses
    assert responses["cmd8_start_cleared"] & 0x80000000==0,responses
    assert responses["cmd8_irq_raw"] & 0x4==0x4,responses
    report={"sdmmc_base":hex(BASE),"sdcard_image_sha256":
             hashlib.sha256(args.sd_image.read_bytes()).hexdigest(),
            "rom_sha256":hashlib.sha256(args.rom.read_bytes()).hexdigest(),
            "qemu_sha256":hashlib.sha256(qemu.read_bytes()).hexdigest(),
            "controller_reset_verified":True,
            "block_size_explicitly_configured":True,
            "cmd0_go_idle_irq_verified":True,
            "cmd8_response_received":responses["cmd8_response"],
            "cmd8_irq_verified":True,
            "registers":{k:hex(v) for k,v in responses.items()},
            "sd_card_attached":True,
            "fat_mount_verified":False,
            "dma_sector_verified":False,
            "board_power_gate_gpio5_verified":False,
            "real_software_boot_verified":False}
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report,indent=2))

if __name__=="__main__":
    main()
