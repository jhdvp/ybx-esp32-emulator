#!/usr/bin/env python3
"""Real compiled QEMU: opt-in ESP32-S3 GP-SPI2 PIO registers and bytes.

The scaffold has no DMA, no EPD panel slave, no BUSY/RESET logic.
A QTest+QMP probe proves only MMIO register/PIO transaction behavior.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from probe_x4pro_touch_injection_qtest import qmp_connect, QTest

BASE=0x60024000
MMIO={
    "cmd":0x0,"user":0x10,"user2":0x18,
    "ms_dlen":0x1c,"w0":0x98
}

def run_model(qemu:Path,rom:Path,flag:bool):
    with TemporaryDirectory(prefix="ybx-gpspi2-qtest-") as temp:
        path=Path(temp)
        bios=path/"rom"
        bios.mkdir()
        (bios/"esp32s3_rev0_rom.bin").write_bytes(rom.read_bytes())
        socket=path/"qmp.sock"
        machine="esp32s3,ybx-x4pro-spi2=on" if flag else "esp32s3"
        args=[
            str(qemu),"-machine",machine,"-accel","qtest",
            "-qtest","stdio","-qmp",f"unix:{socket},server=on,wait=off",
            "-nographic","-monitor","none","-serial","null","-L",str(bios)
        ]
        with (path/"qemu.stderr").open("w") as err:
            process=subprocess.Popen(args,stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE,stderr=err,
                           text=True,bufsize=1)
            client=None
            try:
                client,reader,qmp=qmp_connect(socket)
                qtest=QTest(process)
                assert qtest.rd(BASE+MMIO["cmd"])==0
                if flag:
                    prop=qmp("qom-list",{"path":"/machine/soc/spi2"})
                    assert "return" in prop,prop
                    names={x["name"] for x in prop["return"]}
                    assert {"bytes-sent","transactions","last-byte"} <=names,names
                    qtest.wr(BASE+MMIO["cmd"],1<<23) # UPDATE register sync
                    assert qtest.rd(BASE+MMIO["cmd"]) & (1<<23)==0
                    qtest.wr(BASE+MMIO["w0"],0x44332211)
                    qtest.wr(BASE+MMIO["ms_dlen"],31)
                    qtest.wr(BASE+MMIO["user"],1<<27)
                    qtest.wr(BASE+MMIO["cmd"],1<<24)
                    assert qtest.rd(BASE+MMIO["cmd"]) & (1<<24)==0
                    assert qtest.rd(BASE+MMIO["w0"])==0x44332211
                    counters={}
                    for prop in ["bytes-sent","transactions","last-byte"]:
                        result=qmp("qom-get",{"path":"/machine/soc/spi2",
                                               "property":prop})
                        assert "return" in result,result
                        counters[prop]=result["return"]
                    assert counters=={"bytes-sent":4,"transactions":1,
                                      "last-byte":0x44},counters
                    # No MOSI phase must not create any transfer.
                    qtest.wr(BASE+MMIO["user"],0)
                    qtest.wr(BASE+MMIO["cmd"],1<<24)
                    assert qmp("qom-get",{"path":"/machine/soc/spi2",
                                        "property":"bytes-sent"})["return"]==4
                    return {"qtest_commands":qtest.commands,
                            "spi2_mmio_addr":hex(BASE),
                            "spi2_register_byte_order":"11 22 33 44",
                            "pio_counters":counters,
                            "no_mosi_no_transfer":True,
                            "update_bit_self_clears":True,
                            "epd_panel_verified":False,
                            "dma_gdma_verified":False}
                else:
                    # Generic machine exposes no GP-SPI2 child.
                    result=qmp("qom-list",{"path":"/machine/soc"})
                    assert "return" in result,result
                    assert "spi2" not in {v["name"] for v in result["return"]}
                    return {"qtest_commands":qtest.commands,
                            "spi2_device_absent":True}
            finally:
                if client:client.close()
                process.terminate()
                try:process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--qemu-bin",type=Path,required=True)
    ap.add_argument("--rom",type=Path,required=True)
    ap.add_argument("--out",type=Path,required=True)
    a=ap.parse_args()
    qemu=a.qemu_bin.resolve(strict=True)
    rom=a.rom.resolve(strict=True)
    records={
        "enabled":run_model(qemu,rom,True),
        "default":run_model(qemu,rom,False),
        "qemu_sha256":hashlib.sha256(qemu.read_bytes()).hexdigest(),
        "rom_sha256":hashlib.sha256(rom.read_bytes()).hexdigest(),
        "real_epaper_hardware_verified":False,
        "full_ybx_ui_verified":False,
    }
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(records,indent=2)+"\n")
    print(json.dumps(records,indent=2))

if __name__=="__main__":main()
