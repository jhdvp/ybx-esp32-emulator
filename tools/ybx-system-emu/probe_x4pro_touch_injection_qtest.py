#!/usr/bin/env python3
"""Real QTest + QMP test of virtual GT911 press/release/home and GPIO10 feedback.

Runs INSIDE the isolated offline Docker QEMU builder. QMP controls simulated
sensor state (no fabricated hardware registers); QTest drives actual ESP32-S3
GPIO/I2C MMIO and steps the virtual clock deterministically.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
from tempfile import TemporaryDirectory
import time

GPIO=0x60004000
I2C=0x60013000
ADDR=0x5d

def qmp_connect(path:Path):
    client=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    deadline=time.monotonic()+5
    while True:
        try:
            client.connect(str(path))
            break
        except (ConnectionRefusedError,FileNotFoundError):
            if time.monotonic()>deadline:raise
            time.sleep(.02)
    reader=client.makefile("r",encoding="utf-8",newline="\n")
    greeting=json.loads(reader.readline())
    assert "QMP" in greeting,greeting
    def execute(cmd,args=None):
        payload={"execute":cmd}
        if args is not None:payload["arguments"]=args
        client.sendall((json.dumps(payload)+"\n").encode())
        while True:
            line=reader.readline()
            if not line:raise RuntimeError("QMP closed")
            data=json.loads(line)
            if "event" in data:continue
            return data
    assert "return" in execute("qmp_capabilities")
    return client,reader,execute

class QTest:
    def __init__(self,proc):
        self.proc=proc
        self.commands=0
    def raw(self,command):
        self.proc.stdin.write(command+"\n")
        self.proc.stdin.flush()
        value=self.proc.stdout.readline().strip()
        self.commands+=1
        if not value.startswith("OK"):
            raise AssertionError(f"QTest {command!r}: {value}")
        return int(value.split()[1],0) if len(value.split())>1 else None
    def wr(self,addr,value): self.raw(f"writel 0x{addr:x} 0x{value:x}")
    def rd(self,addr):return self.raw(f"readl 0x{addr:x}")
    def tick(self,nanoseconds):return self.raw(f"clock_step {nanoseconds}")
    def i2c_send(self,addr,buf):
        assert len(buf)<32
        self.wr(I2C+0x18,0x3000)
        self.wr(I2C+0x24,0xffffffff)
        self.wr(I2C+0x04,0x10)
        self.wr(I2C+0x58,0x3000)
        for byte in [addr<<1,*buf]:
            self.wr(I2C+0x1c,byte)
        self.wr(I2C+0x5c,0x900 | (len(buf)+1))
        self.wr(I2C+0x60,0x1000)
        self.wr(I2C+0x04,0x30)
        return self.rd(I2C+0x20)
    def i2c_read(self,addr,reg,count):
        status=self.i2c_send(addr,[reg>>8,reg&0xff])
        assert status==0x80,("write status",hex(status))
        self.wr(I2C+0x18,0x3000)
        self.wr(I2C+0x24,0xffffffff)
        self.wr(I2C+0x04,0x10)
        self.wr(I2C+0x58,0x3000)
        self.wr(I2C+0x1c,(addr<<1)|1)
        self.wr(I2C+0x5c,0x901)
        self.wr(I2C+0x60,0x1800 | count)
        self.wr(I2C+0x64,0x1000)
        self.wr(I2C+0x04,0x30)
        status=self.rd(I2C+0x20)
        assert status==0x80,("read status",hex(status))
        return [self.rd(I2C+0x1c) for _ in range(count)]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu-bin",type=Path,required=True)
    parser.add_argument("--rom",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--gpio-irq-probe",action="store_true",
                        help="requires experimental GPIO IRQ patch 0020-0022")
    args=parser.parse_args()
    with TemporaryDirectory(prefix="ybx-qtest-touch-") as tmp:
        scratch=Path(tmp)
        rom_dir=scratch/"rom"
        rom_dir.mkdir()
        (rom_dir/"esp32s3_rev0_rom.bin").write_bytes(args.rom.read_bytes())
        sock=scratch/"qmp.sock"
        command=[
            str(args.qemu_bin),"-machine",
            "esp32s3,ybx-x4pro-i2c=on,ybx-x4pro-gt911=on",
            "-accel","qtest","-qtest","stdio",
            "-qmp",f"unix:{sock},server=on,wait=off",
            "-nographic","-monitor","none","-serial","null",
            "-L",str(rom_dir)
        ]
        with (scratch/"qemu.stderr").open("w") as err:
            proc=subprocess.Popen(command,stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=err,text=True,bufsize=1)
            client=None
            try:
                client,reader,execute=qmp_connect(sock)
                q=QTest(proc)
                info=execute("qom-list",{"path":"/machine/soc"})
                if "error" in info:
                    raise AssertionError("SoC QOM unavailable "+str(info))
                properties={item["name"] for item in info["return"]}
                need={"ybx-touch-x","ybx-touch-y","ybx-touch-state"}
                assert need<=properties,properties

                def inject(property,value,expect_error=False):
                    r=execute("qom-set",{"path":"/machine/soc",
                        "property":"ybx-touch-"+property,"value":value})
                    if expect_error:
                        assert "error" in r,(property,value,r)
                    else:
                        assert "return" in r,(property,value,r)
                    return r
                def status():
                    return q.i2c_read(ADDR,0x814e,1)[0]
                def assert_int(expected):
                    observed=bool(q.rd(GPIO+0x3c)&(1<<10))
                    assert observed==expected,(observed,expected)
                def irq_level():
                    r=execute("qom-get",{"path":"/machine/soc/gpio",
                                 "property":"ybx-gpio-irq-level"})
                    assert "return" in r,r
                    return r["return"]
                # Real X4Pro side/power buttons have passive pullups.
                # On reset idle GPIO0/3/7 must be HIGH, else stock
                # firmware sees all three as permanently held.
                keys=(1<<0)|(1<<3)|(1<<7)
                assert q.rd(GPIO+0x3c)&keys==keys
                # Same genuine power-on + reset sequence as FreeInk SDK.
                q.wr(GPIO+0x24,0x416)
                assert q.i2c_send(ADDR,[0x81,0x40]) == 0x400
                q.wr(GPIO+0x08,1<<1)  # master rail GPIO1 HIGH
                q.tick(3000000)
                q.wr(GPIO+0x08,0x10)
                q.tick(60000000)
                assert status()==0
                q.wr(GPIO+0x28,1<<10)  # Host returns GPIO10 to input.
                assert_int(False)
                # ESP-IDF GPIO_PIN10_REG: CPU0 INT_ENA bit13 and BOTHEDGES
                # INT_TYPE=3 at bits9:7. Status is GPIO_STATUS_REG 0x44,
                # cleared via GPIO_STATUS_W1TC_REG 0x4C.
                if args.gpio_irq_probe:
                    q.wr(GPIO+0x9c,(1<<13)|(3<<7))
                    q.wr(GPIO+0x4c,1<<10)
                    assert q.rd(GPIO+0x44)&(1<<10)==0
                    assert irq_level()==0

                inject("x",120)
                inject("y",640)
                inject("state",1)  # single contact; ready + IRQ
                assert status()==0x81,status()
                assert_int(True)
                if args.gpio_irq_probe:
                    assert q.rd(GPIO+0x44)&(1<<10)
                    assert irq_level()==1
                    q.wr(GPIO+0x4c,1<<10)
                    assert q.rd(GPIO+0x44)&(1<<10)==0
                    assert irq_level()==0
                point=q.i2c_read(ADDR,0x8150,8)
                assert point[:4]==[120,0,128,2],point
                assert q.i2c_send(ADDR,[0x81,0x4e,0])==0x80
                assert status()==0
                assert_int(False)
                if args.gpio_irq_probe:
                    # Falling edge is independently captured by GPIO_STATUS.
                    assert q.rd(GPIO+0x44)&(1<<10)
                    q.wr(GPIO+0x4c,1<<10)
                    assert irq_level()==0

                inject("state",0)  # release frame: ready but no contact
                assert status()==0x80
                assert_int(True)
                q.i2c_send(ADDR,[0x81,0x4e,0])
                assert_int(False)

                inject("state",2) # HOME key down (status 0x90)
                assert status()==0x90
                assert_int(True)
                q.i2c_send(ADDR,[0x81,0x4e,0])
                inject("state",3) # HOME key up (status 0x80)
                assert status()==0x80
                q.i2c_send(ADDR,[0x81,0x4e,0])
                assert_int(False)

                inject("x",600)
                inject("state",1,expect_error=True) # out of raw range
                inject("x",120)
                q.wr(GPIO+0x24,1<<2)
                q.wr(GPIO+0x08,1<<2)   # power off
                inject("state",1,expect_error=True)
                assert q.i2c_send(ADDR,[0x81,0x4e])==0x400
                assert_int(False)

                guest_readback={}
                for prop in ("status-reads","point-reads",
                             "status-clears","injected-frames"):
                    metric=execute("qom-get",{"path":"/machine/soc",
                                         "property":"ybx-touch-"+prop})
                    assert "return" in metric, (prop,metric)
                    guest_readback[prop]=metric["return"]
                assert guest_readback["status-reads"]>=5,guest_readback
                assert guest_readback["point-reads"]>=1,guest_readback
                assert guest_readback["status-clears"]>=4,guest_readback
                assert guest_readback["injected-frames"]>=4,guest_readback

                report={
                    "i2c_access_counters_qtest":guest_readback,
                    "qtest_mmio_commands":q.commands,
                    "dynamic_qmp_injection":True,
                    "injection_is_simulated_only":True,
                    "qmp_coordinate_range_validation":True,
                    "contact_press_status_0x81":True,
                    "contact_release_status_0x80":True,
                    "home_press_status_0x90":True,
                    "home_release_status_0x80":True,
                    "touch_xy_raw":[120,640],
                    "touch_record_bytes":point,
                    "gpio10_input_feedback":True,
                    "status_clear_deasserts_gpio10":True,
                    "s3_gpio10_edge_status_w1tc":bool(args.gpio_irq_probe),
                    "s3_gpio10_irq_line_level":bool(args.gpio_irq_probe),
                    "x4pro_idle_active_low_buttons_pulled_high":True,
                    "power_off_rejects_touch_injection":True,
                    "power_off_i2c_nack":True,
                    "gpio1_master_rail_required":True,
                    "real_hardware_or_ui_verified":False,
                    "qemu_sha256":hashlib.sha256(args.qemu_bin.read_bytes()).hexdigest(),
                    "rom_sha256":hashlib.sha256(args.rom.read_bytes()).hexdigest(),
                }
                args.out.parent.mkdir(parents=True,exist_ok=True)
                args.out.write_text(json.dumps(report,indent=2)+"\n")
                print(json.dumps(report,indent=2),flush=True)
            finally:
                if client:client.close()
                proc.terminate()
                try:proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                if proc.returncode not in (0,-15,-9):
                    print("QEMU process exit",proc.returncode,
                          (scratch/"qemu.stderr").read_text()[-1300:])

if __name__=="__main__":
    main()
