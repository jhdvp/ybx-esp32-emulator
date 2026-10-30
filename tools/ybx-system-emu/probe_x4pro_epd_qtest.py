#!/usr/bin/env python3
"""Live QTest/QMP of opt-in X4 Pro SSD1677 panel on genuine GP-SPI2.

Distinguishes test-injected commands (not guest CPU) from firmware-driven EPD.
No physical EPD waveform, no firmware acceptance or DMA.
"""
import argparse
import hashlib
import json
import shutil
import struct
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from probe_x4pro_touch_injection_qtest import QTest, qmp_connect

SPI=0x60024000
GPIO=0x60004000
W=800
H=480
SIZE=W*H//8
PINS=(1<<13)|(1<<18)|(1<<14)

def get(qmp,prop):
    res=qmp("qom-get",{"path":"/machine/soc","property":"ybx-epd-"+prop})
    assert "return" in res,(prop,res)
    return res["return"]

def send(qtest,buf,dc):
    assert buf and len(buf)<=64
    qtest.wr(GPIO+(0x08 if dc else 0x0c),1<<18)
    qtest.wr(SPI+0x10,1<<27)
    qtest.wr(SPI+0x1c,len(buf)*8-1)
    for i in range(0,len(buf),4):
        qtest.wr(SPI+0x98+i, int.from_bytes(bytes(buf[i:i+4]).ljust(4,b"\x00"),"little"))
    qtest.wr(SPI+0x00,1<<24)

def save_bmp(path,frame):
    assert len(frame)==SIZE
    # BMP bottom-up, standard 1bpp palette 0=black, 1=white.
    header=bytearray(62)
    header[:2]=b"BM"
    struct.pack_into("<I",header,2,62+SIZE)
    struct.pack_into("<I",header,10,62)
    struct.pack_into("<I",header,14,40)
    struct.pack_into("<ii",header,18,W,H)
    struct.pack_into("<H",header,26,1)
    struct.pack_into("<H",header,28,1)
    struct.pack_into("<I",header,34,SIZE)
    header[58:62]=b"\xff\xff\xff\x00"
    rowbytes=W//8
    payload=b"".join(frame[y*rowbytes:(y+1)*rowbytes]
                     for y in range(H-1,-1,-1))
    path.write_bytes(bytes(header)+payload)

def run(qemu,rom,outbmp,irq_probe=False):
    with TemporaryDirectory(prefix="ybx-ssd1677-qtest-") as td:
        temp=Path(td)
        bios=temp/"rom";bios.mkdir()
        shutil.copyfile(rom,bios/"esp32s3_rev0_rom.bin")
        socket=temp/"qmp.sock"
        cmd=[str(qemu),"-machine",
             "esp32s3,ybx-x4pro-spi2=on,ybx-x4pro-epd=on",
             "-accel","qtest","-qtest","stdio",
             "-qmp",f"unix:{socket},server=on,wait=off",
             "-nographic","-monitor","none","-serial","null","-L",str(bios)]
        with (temp/"qemu.stderr").open("w") as stderr:
            process=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                     stderr=stderr,text=True,bufsize=1)
            client=None
            try:
                client,reader,qmp=qmp_connect(socket)
                qt=QTest(process)
                assert get(qmp,"frames")==0
                # BUSY is active-HIGH, GPIO6 high-level interrupt can be
                # configured by the guest. It MUST deassert automatically
                # when the panel releases BUSY, unlike an edge IRQ.
                if irq_probe:
                    qt.wr(GPIO+0x8c,(1<<13)|(5<<7))
                    assert qt.rd(GPIO+0x44)&(1<<6)==0
                def gpio_irq():
                    r=qmp("qom-get",{"path":"/machine/soc/gpio",
                                      "property":"ybx-gpio-irq-level"})
                    assert "return" in r,r
                    return r["return"]
                if irq_probe:
                    assert gpio_irq()==0
                # All three board output pins: default driven high before enable
                qt.wr(GPIO+0x08,PINS)
                qt.wr(GPIO+0x24,PINS)
                # D/C high and CS high -> ignored.
                send(qt,[0x24],dc=False)
                assert get(qmp,"commands")==0, "CS high incorrectly accepted"
                qt.wr(GPIO+0x0c,1<<13)
                # Program SSD1677 portrait-independent 800x480 RAM window.
                for command,params in (
                    (0x11,[0x03]),(0x44,[0,0,31,3]),(0x45,[0,0,223,1]),
                    (0x4e,[0,0]),(0x4f,[0,0])
                ):
                    send(qt,[command],dc=False)
                    send(qt,params,dc=True)
                send(qt,[0x24],dc=False)
                pattern=bytes((0,0x11,0x22,0x33,0x44,0x55,0x66,0x77))
                send(qt,pattern,dc=True)
                assert get(qmp,"ram-writes")==8,get(qmp,"ram-writes")
                assert get(qmp,"frames")==0
                # 0x20 also powers on (0xC0) and off (0x03) without painting.
                send(qt,[0x22],dc=False)
                send(qt,[0xc0],dc=True)
                send(qt,[0x20],dc=False)
                assert get(qmp,"frames")==0
                qt.tick(351_000_000)
                # Only CTRL2 DISPLAY_START bit2 counts as displayed frame.
                send(qt,[0x22],dc=False)
                send(qt,[0xf7],dc=True)
                send(qt,[0x20],dc=False)
                assert get(qmp,"frames")==1
                assert get(qmp,"busy")==1
                assert qt.rd(GPIO+0x3c)&(1<<6)
                if irq_probe:
                    assert qt.rd(GPIO+0x44)&(1<<6)
                    assert gpio_irq()==1
                assert get(qmp,"nonwhite")==8
                frame=bytes.fromhex(get(qmp,"visible-hex"))
                assert len(frame)==SIZE
                assert frame[:8]==pattern
                assert all(x==0xff for x in frame[8:])
                qt.tick(351_000_000)
                assert get(qmp,"busy")==0
                assert qt.rd(GPIO+0x3c)&(1<<6)==0
                if irq_probe:
                    assert qt.rd(GPIO+0x44)&(1<<6)==0
                    assert gpio_irq()==0
                    # Real ESP-IDF gpio_ll_get_intr_status() on ESP32-S3
                    # READS GPIO_PCPU_INT_REG (0x5c), not GPIO_STATUS (0x44).
                    # Verify both BUSY transitions with CHANGE edge IRQ,
                    # acknowledgment using GPIO_STATUS_W1TC_REG (0x4c).
                    qt.wr(GPIO+0x8c,(1<<13)|(3<<7))
                    qt.wr(GPIO+0x4c,1<<6)
                    send(qt,[0x22],dc=False)
                    send(qt,[0xc0],dc=True)  # power-on only, NOT refresh
                    send(qt,[0x20],dc=False)
                    assert get(qmp,"busy")==1
                    assert qt.rd(GPIO+0x5c)&(1<<6), "PCPU_INT missed rising BUSY edge"
                    assert gpio_irq()==1
                    qt.wr(GPIO+0x4c,1<<6)
                    assert qt.rd(GPIO+0x5c)&(1<<6)==0
                    assert gpio_irq()==0
                    qt.tick(351_000_000)
                    assert get(qmp,"busy")==0
                    assert qt.rd(GPIO+0x5c)&(1<<6), "PCPU_INT missed falling BUSY edge"
                    assert gpio_irq()==1
                    qt.wr(GPIO+0x4c,1<<6)
                    assert qt.rd(GPIO+0x5c)&(1<<6)==0
                    assert gpio_irq()==0
                # SSD1677 addresses X in PIXELS: x=792 is physical byte99;
                # next MOSI byte wraps X and advances Y to the next row.
                for command,params in (
                    (0x11,[0x03]),(0x44,[0,0,31,3]),(0x45,[0,0,1,0]),
                    (0x4e,[24,3]),(0x4f,[0,0])
                ):
                    send(qt,[command],dc=False)
                    send(qt,params,dc=True)
                send(qt,[0x24],dc=False)
                send(qt,[0x0f,0xf0],dc=True)
                send(qt,[0x22],dc=False)
                send(qt,[0xf7],dc=True)
                send(qt,[0x20],dc=False)
                second=bytes.fromhex(get(qmp,"visible-hex"))
                assert second[99]==0x0f and second[100]==0xf0, (second[99],second[100])
                assert get(qmp,"frames")==2
                qt.tick(351_000_000)
                # GPIO14 held reset -> commands and RAM writes ignored.
                qt.wr(GPIO+0x0c,1<<14)
                before=get(qmp,"commands")
                send(qt,[0x24],dc=False)
                assert get(qmp,"commands")==before
                qt.wr(GPIO+0x08,1<<14)
                # CS high again, ignored command
                qt.wr(GPIO+0x08,1<<13)
                send(qt,[0x20],dc=False)
                assert get(qmp,"frames")==2
                save_bmp(outbmp,frame)
                return {"commands":get(qmp,"commands"),
                        "ram_writes_before_reset":8,
                        "frames":get(qmp,"frames"),
                        "busy_pulse_verified":True,
                        "s3_pcpu_int_alias_and_edge_w1tc_verified":bool(irq_probe),
                        "gpio6_level_irq_follows_epd_busy":bool(irq_probe),
                        "non_display_poweron_excluded":True,
                        "ssd1677_x_10bit_pixel_addressing":True,
                        "pixel_to_byte_row_wrap_verified":True,
                        "dc_gpio18_verified":True,"cs_gpio13_verified":True,
                        "reset_gpio14_verified":True,
                        "busy_gpio6_verified":True,
                        "framebuffer":f"{W}x{H} monochrome",
                        "visible_frame_sha256":hashlib.sha256(frame).hexdigest(),
                        "visible_frame_bytes":len(frame),
                        "test_pattern_pixel_bytes":pattern.hex(),
                        "guest_firmware_produced_frame":False,
                        "physical_epaper_verified":False,
                        "qtest_mmio_commands":qt.commands}
            finally:
                if client:client.close()
                process.terminate()
                try:process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()

def main():
    a=argparse.ArgumentParser(description=__doc__)
    a.add_argument("--qemu-bin",type=Path,required=True)
    a.add_argument("--rom",type=Path,required=True)
    a.add_argument("--out",type=Path,required=True)
    a.add_argument("--out-bmp",type=Path,required=True)
    a.add_argument("--gpio-irq-probe",action="store_true",
                   help="requires experimental GPIO IRQ patches 0020-0022")
    arg=a.parse_args()
    res=run(arg.qemu_bin.resolve(strict=True),arg.rom.resolve(strict=True),
            arg.out_bmp.resolve(),arg.gpio_irq_probe)
    res.update({"qemu_sha256":hashlib.sha256(arg.qemu_bin.read_bytes()).hexdigest(),
                "rom_sha256":hashlib.sha256(arg.rom.read_bytes()).hexdigest(),
                "out_bmp":str(arg.out_bmp.resolve())})
    arg.out.parent.mkdir(parents=True,exist_ok=True)
    arg.out.write_text(json.dumps(res,indent=2)+"\n")
    print(json.dumps(res,indent=2))

if __name__=="__main__":main()
