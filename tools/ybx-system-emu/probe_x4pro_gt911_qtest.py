#!/usr/bin/env python3
"""Live compiled-QEMU QTest for optional, power-gated X4 Pro GT911.

No synthetic touches or hardware IRQ events are generated. This verifies
real MMIO GPIO2 power, GPIO4 reset, GPIO10 address selection and I2C NACK/ACK.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import argparse
import hashlib
import json
import re
import shutil
import subprocess
from probe_x4pro_i2c_qtest import Protocol

GPIO = 0x60004000
GT911 = 0x5d

def make_protocol() -> Protocol:
    p = Protocol()
    def gpio(offset, val):
        p.commands.append(f"writel 0x{GPIO+offset:x} 0x{val:x}")
    def tick(ns):
        p.commands.append(f"clock_step {ns}")
    def read_register(addr: int, reg: int, count: int, name: str):
        p.send(addr, [reg >> 8, reg & 0xff], name + ".pointer")
        p.reset()
        p.write(0x1c, (addr << 1) | 1)
        p.write(0x5c, 0x901)
        p.write(0x60, 0x1800 | count)
        p.write(0x64, 0x1000)
        p.write(0x04, 0x30)
        p.read(0x20, name + ".read_status")
        for idx in range(count):
            p.read(0x1c, name + f".byte{idx}")
    p.send(GT911, [0x81, 0x40], "touch.unpowered")
    # GPIO2 active-LOW power; GPIO4 reset LOW; GPIO10 INT LOW => 0x5d.
    gpio(0x24, (1 << 1) | (1 << 2) | (1 << 4) | (1 << 10))
    p.send(GT911, [0x81, 0x40], "touch.master_rail_off")
    gpio(0x08, 1 << 1)       # GPIO1 HIGH enables master rail
    p.send(GT911, [0x81, 0x40], "touch.reset_low")
    tick(3000000)             # minimum 2ms reset low satisfied
    gpio(0x08, 1 << 4)       # release GPIO4 reset HIGH
    p.send(GT911, [0x81, 0x40], "touch.not_ready")
    tick(60000000)            # GT911 self-load/ready >=50ms
    read_register(GT911, 0x8140, 4, "touch.product")
    read_register(GT911, 0x814e, 1, "touch.no_contacts")
    gpio(0x08, 1 << 2)       # GPIO2 HIGH => power off
    p.send(GT911, [0x81, 0x40], "touch.powered_off")
    gpio(0x0c, 1 << 2)       # GPIO2 LOW again, but reset not repeated
    p.send(GT911, [0x81, 0x40], "touch.requires_reset")
    # Select alternate 0x14 by raising INT at reset rising edge.
    gpio(0x08, 1 << 10)
    gpio(0x0c, 1 << 4)
    tick(3000000)
    gpio(0x08, 1 << 4)
    tick(60000000)
    p.send(GT911, [0x81, 0x40], "touch.old_address_nack")
    read_register(0x14, 0x8140, 3, "touch.alternate_address")
    gpio(0x0c, 1 << 1)       # drop master rail while GPIO2 remains LOW
    p.send(0x14, [0x81, 0x40], "touch.master_rail_dropped")
    return p

def validate(p: Protocol, stdout: str):
    if "FAIL " in stdout:
        raise AssertionError("QTest command failure: " + stdout[-700:])
    values=[int(v,16) for v in re.findall(
        r"(?m)^OK\s+0x([0-9a-fA-F]+)\s*$",stdout)]
    if len(values) != len(p.read_names):
        raise AssertionError(f"expected {len(p.read_names)} hex reads, got {len(values)}")
    m=dict(zip(p.read_names,values))
    for name in ["touch.unpowered", "touch.master_rail_off",
                 "touch.master_rail_dropped", "touch.reset_low", "touch.not_ready",
                 "touch.powered_off", "touch.requires_reset",
                 "touch.old_address_nack"]:
        if m[name + ".write_status"] != 0x400:
            raise AssertionError(f"unexpected ACK: {name}={m[name + '.write_status']:#x}")
    for name in ["touch.product", "touch.no_contacts", "touch.alternate_address"]:
        if m[name+".pointer.write_status"] != 0x80 or m[name+".read_status"] != 0x80:
            raise AssertionError(f"expected ACK for {name}")
    for i,value in enumerate([ord('9'),ord('1'),ord('1'),0]):
        assert m[f"touch.product.byte{i}"]==value
    assert m["touch.no_contacts.byte0"]==0
    for i,value in enumerate([ord('9'),ord('1'),ord('1')]):
        assert m[f"touch.alternate_address.byte{i}"]==value
    return {
        "power_off_nack":True, "master_rail_gpio1_off_nack":True,
        "master_rail_gpio1_drop_nack":True, "hold_reset_nack":True,
        "reset_delay_required":True, "gt911_product_id":"911",
        "idle_touch_count":0, "power_cycle_requires_new_reset":True,
        "alternate_address_0x14":True, "touch_event_injection":False,
        "gpio10_interrupt_input_feedback":False,
        "real_touch_hardware_verified":False,
        "boot_and_epaper_verified":False,
        "read_count":len(values),
    }

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu-root",type=Path,required=True)
    parser.add_argument("--rom",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--sd-power",action="store_true",
                        help="also enable X4 Pro SD slot GPIO1/5 power model")
    parser.add_argument("--sd-image",type=Path,
                        help="required with --sd-power, disposable test FAT image")
    parser.add_argument("--docker-image",default="ybx-qemu-builder:20261029")
    a=parser.parse_args()
    qemu=(a.qemu_root/"build-ybx/qemu-system-xtensa").resolve(strict=True)
    if a.sd_power and a.sd_image is None:
        parser.error("--sd-power requires --sd-image")
    p=make_protocol()
    with TemporaryDirectory(prefix="ybx-gt911-qtest-") as temp:
        shutil.copyfile(a.rom,Path(temp)/"esp32s3_rev0_rom.bin")
        if a.sd_power:
            shutil.copyfile(a.sd_image,Path(temp)/"sd.img")
        machine = "esp32s3,ybx-x4pro-i2c=on,ybx-x4pro-gt911=on"
        if a.sd_power:
            machine += ",ybx-x4pro-sd-power=on"
        cmd=["docker","run","--rm","-i","--network","none",
             "--platform","linux/arm64",
             "-v",f"{qemu.parent.parent}:/qemu:ro",
             "-v",f"{temp}:/bios"+("" if a.sd_power else ":ro"),
             a.docker_image,
             "timeout","-k","2s","7s",
             "/qemu/build-ybx/qemu-system-xtensa",
             "-machine",machine,
             "-accel","qtest",
             "-nographic","-monitor","none","-serial","null",
             "-qtest","stdio","-L","/bios"]
        if a.sd_power:
            cmd += ["-drive","file=/bios/sd.img,if=sd,format=raw"]
        result=subprocess.run(cmd,input=p.text(),text=True,
                              capture_output=True,timeout=25)
        if result.returncode not in (0,124):
            raise RuntimeError(f"QEMU exit={result.returncode}: {result.stderr[-900:]}")
        report=validate(p,result.stdout)
    report["qemu_sha256"]=hashlib.sha256(qemu.read_bytes()).hexdigest()
    report["rom_sha256"]=hashlib.sha256(a.rom.read_bytes()).hexdigest()
    report["qtest_only"]=True
    report["combined_sd_power_enabled"]=a.sd_power
    report["sd_card_fat_mounted_by_guest"]=False
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report,indent=2))

if __name__=="__main__":
    main()
