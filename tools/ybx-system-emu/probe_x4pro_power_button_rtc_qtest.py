#!/usr/bin/env python3
"""QTest: X4 Pro active-low POWER drives both digital GPIO3 and RTC EXT1.

The board event is injected at its *button* QMP property, not at the RTC.
Normal digital input and guest-selected RTC wake must agree on one level.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from probe_x4pro_touch_injection_qtest import QTest, qmp_connect

RTC=0x60008000
GPIO=0x60004000
STATE0,WAKEUP,CAUSE=0x18,0x3c,0x38
CONF,SEL,STATUS=0x64,0xe0,0xe4
SLEEP,EXT1,ANY_HIGH=(1<<31),(1<<16),(1<<31)


def run(qemu:Path,rom:Path):
    with TemporaryDirectory(prefix='ybx-x4pro-power-rtc-') as td:
        root=Path(td)
        bios=root/'rom';bios.mkdir()
        shutil.copyfile(rom,bios/'esp32s3_rev0_rom.bin')
        sock=root/'qmp.sock'
        cmd=[str(qemu),'-machine','esp32s3,ybx-x4pro-i2c=on,ybx-x4pro-gt911=on',
             '-accel','qtest','-qtest','stdio',
             '-qmp',f'unix:{sock},server=on,wait=off',
             '-nographic','-monitor','none','-serial','null','-L',str(bios)]
        with (root/'stderr').open('w') as log:
            proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                  stderr=log,text=True,bufsize=1)
            client=None
            try:
                client,reader,qmp=qmp_connect(sock)
                qt=QTest(proc)
                def wr(off,val):qt.wr(RTC+off,val)
                def rd(off):return qt.rd(RTC+off)
                def get(prop):
                    r=qmp('qom-get',{'path':'/machine/soc','property':prop})
                    assert 'return' in r,r
                    return r['return']
                def press(pressed):
                    r=qmp('qom-set',{'path':'/machine/soc',
                        'property':'ybx-x4pro-power-button-pressed','value':pressed})
                    assert 'return' in r,r
                assert qt.rd(GPIO+0x3c)&(1<<3), 'GPIO3 should idle HIGH'
                press(False)  # explicit known idle high on both domains
                assert get('ybx-rtc-ext1-pad-valid')&(1<<3)
                assert get('ybx-rtc-ext1-pad-high')&(1<<3)
                assert get('ybx-x4pro-power-button-pressed') is False
                assert qt.rd(GPIO+0x3c)&(1<<3)
                assert get('ybx-rtc-ext1-wake-events') == 0
                press(True) # not in sleep: cannot wake
                assert qt.rd(GPIO+0x3c)&(1<<3) == 0
                assert get('ybx-rtc-ext1-pad-high')&(1<<3) == 0
                assert get('ybx-rtc-ext1-wake-events') == 0
                press(False)
                wr(SEL,1<<7)
                wr(CONF,0)
                wr(WAKEUP,EXT1)
                wr(STATE0,SLEEP)
                press(True)  # wrong selected pin 7, not GPIO3
                assert rd(STATE0)&SLEEP
                assert get('ybx-rtc-ext1-wake-events') == 0
                press(False)
                wr(STATE0,0)
                wr(SEL,1<<3)
                press(True)  # input LOW before enabling ANY_HIGH sleep
                wr(CONF,ANY_HIGH)
                wr(STATE0,SLEEP)
                press(True)  # correct pin LOW, wrong level mode (idempotent)
                assert get('ybx-rtc-ext1-wake-events') == 0
                wr(STATE0,0)  # disarm before releasing button HIGH
                press(False)
                wr(CONF,0)
                wr(WAKEUP,0)
                wr(STATE0,SLEEP)
                press(True) # correct pin, disabled wake
                assert get('ybx-rtc-ext1-wake-events') == 0
                press(False)
                wr(STATE0,0)
                wr(WAKEUP,EXT1)
                wr(STATE0,SLEEP)
                assert get('ybx-rtc-ext1-wake-events') == 0
                press(True)  # guest-configured GPIO3 ALL_LOW + real board input
                qt.tick(1000000)
                assert qt.rd(GPIO+0x3c)&(1<<3) == 0
                assert get('ybx-rtc-ext1-wake-events') == 1
                assert rd(STATUS)&(1<<3)
                assert rd(STATE0)&SLEEP == 0
                reset=rd(CAUSE)
                assert (reset&0x3f)==5 and ((reset>>6)&0x3f)==5,hex(reset)
                press(False)
                assert qt.rd(GPIO+0x3c)&(1<<3)
                assert get('ybx-x4pro-power-button-pressed') is False
                assert get('ybx-rtc-ext1-wake-events') == 1
                return {'result':'PASS','qtest_commands':qt.commands,
                        'rtc_ext1_resets':get('ybx-rtc-ext1-wake-events'),
                        'button_changes':get('ybx-x4pro-power-button-changes'),
                        'reset_state':hex(reset),
                        'gpio3_to_rtc3_wiring_tested':True,
                        'real_physical_button_verified':False}
            finally:
                if client:client.close()
                proc.terminate()
                try:proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:proc.kill();proc.communicate()


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--qemu-bin',type=Path,required=True)
    ap.add_argument('--rom',type=Path,required=True)
    ap.add_argument('--out',type=Path)
    args=ap.parse_args()
    result=run(args.qemu_bin,args.rom)
    print(json.dumps(result,indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True,exist_ok=True)
        args.out.write_text(json.dumps(result,indent=2)+'\n')
