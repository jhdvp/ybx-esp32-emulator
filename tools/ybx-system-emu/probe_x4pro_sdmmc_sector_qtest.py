#!/usr/bin/env python3
"""Native SDMMC QTest exploration: initialize card and DMA-read LBA0.

This runs solely on a disposable FAT image and never modifies production
firmware or user media. Does not imply ESP-IDF/FAT filesystem mount.
"""
import argparse
import hashlib
import json
import subprocess
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from probe_x4pro_touch_injection_qtest import QTest

BASE=0x60028000
DRAM_DESC=0x3fc80000
DRAM_BUFFER=0x3fc81000
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--qemu-bin",type=Path,required=True)
    p.add_argument("--rom",type=Path,required=True)
    p.add_argument("--sd-image",type=Path,required=True)
    p.add_argument("--out",type=Path,required=True)
    a=p.parse_args()
    with TemporaryDirectory(prefix="ybx-sdmmc-dma-") as td:
        root=Path(td)
        (root/"rom").mkdir()
        shutil.copyfile(a.rom,root/"rom/esp32s3_rev0_rom.bin")
        sd=root/"sd.img"
        shutil.copyfile(a.sd_image,sd)
        cmd=[str(a.qemu_bin),"-machine","esp32s3","-accel","qtest",
             "-qtest","stdio","-nographic","-monitor","none",
             "-serial","null","-L",str(root/"rom"),
             "-drive",f"file={sd},if=sd,format=raw"]
        with (root/"qemu.err").open("w") as err:
            proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                    stderr=err,text=True,bufsize=1)
            def wr(q,reg,val):q.wr(BASE+reg,val)
            def rd(q,reg):return q.rd(BASE+reg)
            report={"cmds":[],"card_initialized":False,"dma_sector_verified":False}
            try:
                q=QTest(proc)
                wr(q,0x00,0x7)     # controller reset
                wr(q,0x00,0x30)    # IRQ/DMA enable
                wr(q,0x1c,512)    # block size
                def card(cmdno,arg=0,resp=True,long=False,data=False,write=False):
                    wr(q,0x44,0xffffffff)
                    wr(q,0x28,arg)
                    cfg=0x80000000|cmdno
                    if resp:cfg|=0x40
                    if long:cfg|=0x80
                    if data:cfg|=0x200
                    if write:cfg|=0x400
                    wr(q,0x2c,cfg)
                    raw=rd(q,0x44)
                    value=rd(q,0x30)
                    report["cmds"].append({"cmd":cmdno,"arg":hex(arg),
                                            "irq":hex(raw),"r0":hex(value)})
                    return value,raw
                card(0,resp=False)
                r,_=card(8,0x1aa)
                assert r&0xfff==0x1aa,hex(r)
                for _ in range(4):
                    card(55)
                    ocr,_=card(41,0x40ff8000)
                    if ocr & 0x80000000:break
                else:
                    raise AssertionError("SD ACMD41 failed to become ready")
                card(2,long=True)
                r,_=card(3)
                rca=(r>>16)&0xffff
                assert rca,report
                card(7,rca<<16)
                card(16,512)
                report["card_initialized"]=True
                report["ocr"]=hex(ocr)
                report["relative_card_address"]=hex(rca)
                # Hardware IDMAC descriptor: OWN | FIRST | LAST.
                q.wr(DRAM_DESC,0x8000000c)
                q.wr(DRAM_DESC+4,512)
                q.wr(DRAM_DESC+8,DRAM_BUFFER)
                q.wr(DRAM_DESC+12,0)
                wr(q,0x88,DRAM_DESC)
                wr(q,0x20,512)
                card(17,0,data=True)
                report["data_over_irq"]=hex(rd(q,0x44))
                report["idmac_status"]=hex(rd(q,0x8c))
                words=[q.rd(DRAM_BUFFER+4*i) for i in range(128)]
                actual=b"".join(x.to_bytes(4,"little") for x in words)
                expected=a.sd_image.read_bytes()[:512]
                report["dma_sector_verified"]=actual==expected
                report["actual_sha256"]=hashlib.sha256(actual).hexdigest()
                report["expected_sha256"]=hashlib.sha256(expected).hexdigest()
                report["readback_signature"]=actual[510:512].hex()
                if not report["dma_sector_verified"]:
                    raise AssertionError("DMA sector data differs from image LBA0")
                # Additional write/read roundtrip on sector 1 of the DISPOSABLE
                # local copy only; NEVER operate on the source FAT image.
                # OCR CCS bit30 distinguishes block-addressed from byte-addressed.
                sector1_address = 1 if (ocr & 0x40000000) else 512
                pattern=bytes((i*17+37) & 0xff for i in range(512))
                for i in range(128):
                    q.wr(DRAM_BUFFER+4*i,int.from_bytes(pattern[4*i:4*i+4],"little"))
                q.wr(DRAM_DESC,0x8000000c)
                wr(q,0x20,512)
                card(24,sector1_address,data=True,write=True)
                report["dma_write_data_over_irq"]=hex(rd(q,0x44))
                report["dma_write_idmac_status"]=hex(rd(q,0x8c))
                # Restore DMA descriptor ownership, poison buffer and read
                # the same LBA through CMD17. The fresh read must repopulate it.
                q.wr(DRAM_DESC,0x8000000c)
                for i in range(128):q.wr(DRAM_BUFFER+4*i,0xa5a5a5a5)
                wr(q,0x20,512)
                card(17,sector1_address,data=True)
                returned=b"".join(q.rd(DRAM_BUFFER+4*i).to_bytes(
                    4,"little") for i in range(128))
                report["dma_write_readback_verified"]=returned==pattern
                report["dma_write_readback_sha256"]=hashlib.sha256(returned).hexdigest()
                report["dma_expected_pattern_sha256"]=hashlib.sha256(pattern).hexdigest()
                report["write_only_to_disposable_test_copy"]=True
                if returned!=pattern:
                    raise AssertionError("CMD24/CMD17 sector 1 roundtrip failed")
            finally:
                proc.terminate()
                try:proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:proc.kill();proc.communicate()
                report["qemu_stderr_tail"]=(root/"qemu.err").read_text()[-700:]
                report["qtest_operations"]=q.commands if 'q' in locals() else 0
        a.out.parent.mkdir(parents=True,exist_ok=True)
        a.out.write_text(json.dumps(report,indent=2)+"\n")
        print(json.dumps(report,indent=2),flush=True)
if __name__=="__main__":main()
