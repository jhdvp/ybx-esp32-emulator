#!/usr/bin/env python3
"""Original inkMOD: virtual X4 Pro POWER button GPIO3 -> RTC EXT1 DSLEEP.

The input is a BOARD BUTTON press; QEMU must fan it out to digital GPIO and
RTC level. Neither QMP RTC direct wake nor a timer is used to reset the CPU.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from probe_x4pro_touch_injection_qtest import qmp_connect
from probe_inkmod_native_touch import MACHINE, read


def run(a):
    with tempfile.TemporaryDirectory(prefix='inkmod-rtc-ext1-') as tmp:
        root=Path(tmp)
        bios=root/'rom';bios.mkdir()
        shutil.copyfile(a.rom,bios/'esp32s3_rev0_rom.bin')
        shutil.copyfile(a.flash,root/'flash.bin')
        shutil.copyfile(a.sd,root/'sd.img')
        socket=root/'qmp.sock'
        cmd=[str(a.qemu),'-machine',MACHINE,'-m','8M',
             '-global','ssi_psram.is_octal=on','-smp','2',
             '-nographic','-monitor','none',
             '-serial',f'file:{root}/uart0.log', '-serial','null',
             '-serial','null','-serial','null',
             '-qmp',f'unix:{socket},server=on,wait=off','-L',str(bios),
             '-drive',f'file={root}/flash.bin,if=mtd,format=raw',
             '-drive',f'file={root}/sd.img,if=sd,format=raw']
        with (root/'stdout').open('w') as stdout,(root/'stderr').open('w') as stderr:
            proc=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,
                                  stdout=stdout,stderr=stderr)
            client=None
            try:
                client,reader,qmp=qmp_connect(socket)
                start=time.monotonic()
                while time.monotonic()-start<a.wait:
                    if read(qmp,'ybx-rtc-sleep-requests')>=1:
                        break
                    time.sleep(.15)
                sleep_requests=read(qmp,'ybx-rtc-sleep-requests')
                selector=read(qmp,'ybx-rtc-ext1-sel')
                conf=read(qmp,'ybx-rtc-ext1-conf')
                mask=selector&0x3fffff
                high_mode=bool(conf&(1<<31))
                wake_state=qmp('human-monitor-command',{'command-line':
                                             'xp /1wx 0x6000803c'})
                before=read(qmp,'ybx-rtc-ext1-wake-events')
                input_events=[]
                digital_before=qmp('human-monitor-command',{'command-line':
                                                  'xp /1wx 0x6000403c'})
                if a.drive and sleep_requests and mask:
                    if mask != (1<<3) or high_mode:
                        raise RuntimeError('Original inkMOD did not arm POWER GPIO3 ALL_LOW; refusing non-matching input')
                    for pressed in (False, True):
                        reply=qmp('qom-set',{'path':'/machine/soc',
                               'property':'ybx-x4pro-power-button-pressed',
                               'value':pressed})
                        assert 'return' in reply, reply
                        input_events.append({'board_pin':'POWER GPIO3',
                                             'pressed':pressed,
                                             'level_high':not pressed})
                digital_after=qmp('human-monitor-command',{'command-line':
                                                 'xp /1wx 0x6000403c'})
                time.sleep(a.after)
                after=read(qmp,'ybx-rtc-ext1-wake-events')
                frames=read(qmp,'ybx-epd-frames')
                status=read(qmp,'ybx-rtc-ext1-status')
                reset_cause=qmp('human-monitor-command',{'command-line':
                                             'xp /1wx 0x60008038'})
                report={'guest':'original inkMOD v1.1.10','sleep_requests':sleep_requests,
                        'ext1_selector':hex(selector),'selected_pads':
                            [i for i in range(22) if mask&(1<<i)],
                        'ext1_mode':'ANY_HIGH' if high_mode else 'ALL_LOW',
                        'wakeup_state_hmp':wake_state.get('return',wake_state),
                        'ext1_inputs_driven':input_events,
                        'button_qmp_input':'ybx-x4pro-power-button-pressed',
                        'digital_gpio_before':digital_before.get('return'),
                        'digital_gpio_after':digital_after.get('return'),
                        'wake_events_before':before,'wake_events_after':after,
                        'ext1_status':hex(status),'reset_state_hmp':
                            reset_cause.get('return',reset_cause),
                        'epd_frames':frames,
                        'native_ext1_level_woke_guest':after>before and frames>=3}
            finally:
                if client:client.close()
                proc.terminate()
                try:proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:proc.kill();proc.communicate()
        uart=(root/'uart0.log').read_text(errors='replace')
        report['rom_ds1_seen']='rst:0x5 (DSLEEP)' in uart
        report['uart_i2c_invalid_state_count']=uart.count('ESP_ERR_INVALID_STATE')
        report['uart_panic']='abort() was called' in uart or 'Guru Meditation' in uart
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for arg in ('qemu','rom','flash','sd','out'):
        parser.add_argument('--'+arg,type=Path,required=True)
    parser.add_argument('--wait',type=float,default=10)
    parser.add_argument('--after',type=float,default=5)
    parser.add_argument('--drive',action='store_true',
                        help='X4 Pro POWER GPIO3 button press; no direct RTC reset injection')
    run(parser.parse_args())
