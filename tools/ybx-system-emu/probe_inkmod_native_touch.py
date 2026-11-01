#!/usr/bin/env python3
"""Real inkMOD UI: opt-in RTC DSLEEP, native GT911 contacts, SSD1677 frame gates.

Offline, disposable QEMU with private flash and SD. A successful QMP injection
alone is NOT proof of guest input; the verdict requires actual GT911 I2C reads
and a changed *visible* framebuffer. No firmware patching or UI substitution.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from probe_x4pro_touch_injection_qtest import qmp_connect
from probe_x4pro_guest_touch_ui import sample, sensor, touch_metrics

MACHINE = ('esp32s3,ybx-x4pro-i2c=on,ybx-x4pro-gt911=on,'
           'ybx-x4pro-sd-power=on,ybx-x4pro-spi2=on,ybx-x4pro-epd=on')


def read(qmp, prop):
    result = qmp('qom-get', {'path': '/machine/soc', 'property': prop})
    if 'return' not in result:
        raise RuntimeError(f'QMP property {prop} failed: {result}')
    return result['return']


def assess_native_touch(before, after, metrics_delta, injections):
    """Conservative UI gate; QMP acceptance and I2C alone are not navigation."""
    return bool(
        all(value.get('ok') for value in injections.values())
        and before.get('frame_sha256') and after.get('frame_sha256')
        and before['frame_sha256'] != after['frame_sha256']
        and after.get('frames', 0) > before.get('frames', 0)
        and metrics_delta.get('point-reads', 0) > 0
        and metrics_delta.get('status-clears', 0) >= 1
        and metrics_delta.get('injected-frames', 0) >= 2
    )


def classify_sequence(taps):
    """Never call partial, single-step, or QMP-only input full acceptance."""
    if not taps:
        return 'NOT_VERIFIED'
    n = sum(bool(t.get('native_ui_navigation_verified')) for t in taps)
    if n == len(taps):
        return 'PASS'
    return 'PARTIAL' if n else 'NOT_VERIFIED'


def tap(qmp, x: int, y: int, press: float, settle: float, out: Path):
    before = sample(qmp, 'tap-before', out)
    metrics_before = touch_metrics(qmp)
    attempts = {}
    attempts['x'] = sensor(qmp, 'x', x)
    attempts['y'] = sensor(qmp, 'y', y)
    attempts['press'] = sensor(qmp, 'state', 1)
    time.sleep(press)
    during = sample(qmp, 'tap-during', out)
    attempts['release'] = sensor(qmp, 'state', 0)
    time.sleep(settle)
    after = sample(qmp, 'tap-after', out)
    metrics_after = touch_metrics(qmp)
    metrics_delta = {k:metrics_after[k]-metrics_before[k] for k in metrics_before}
    changed = bool(before.get('frame_sha256') and after.get('frame_sha256')
                   and before['frame_sha256'] != after['frame_sha256'])
    refreshed = after['frames'] > before['frames']
    consumed = metrics_delta['status-reads'] > 0 and metrics_delta['point-reads'] > 0
    cleared = metrics_delta['status-clears'] >= 1
    return {'xy':[x,y], 'injections':attempts, 'before':before, 'during':during,
            'after':after, 'gt911_before':metrics_before,'gt911_after':metrics_after,
            'gt911_deltas':metrics_delta,
            'guest_i2c_consumed_touch':consumed,'guest_i2c_cleared_status':cleared,
            'frame_sha_changed':changed,'frame_refresh_after_touch':refreshed,
            'native_ui_navigation_verified':assess_native_touch(
                before,after,metrics_delta,attempts)}


def run(a):
    a.out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ybx-inkmod-native-touch-') as t:
        root = Path(t)
        bios = root/'rom'; bios.mkdir()
        shutil.copyfile(a.rom, bios/'esp32s3_rev0_rom.bin')
        shutil.copyfile(a.flash,root/'flash.bin')
        shutil.copyfile(a.sd,root/'sd.img')
        sock=root/'qmp.sock'
        cmd=[str(a.qemu), '-machine', MACHINE, '-m','8M',
             '-global','ssi_psram.is_octal=on','-smp','2',
             '-nographic','-monitor','none','-serial',f'file:{root}/uart0.log',
             '-serial','null','-serial','null','-serial','null',
             '-qmp',f'unix:{sock},server=on,wait=off', '-L',str(bios),
             '-drive',f'file={root}/flash.bin,if=mtd,format=raw',
             '-drive',f'file={root}/sd.img,if=sd,format=raw']
        with (root/'qemu.stdout').open('w') as stdout, (root/'qemu.stderr').open('w') as stderr:
            proc = subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr)
            client=None
            try:
                client,reader,qmp=qmp_connect(sock)
                since=time.monotonic()
                injected=False
                while time.monotonic()-since < a.boot_limit:
                    if read(qmp,'ybx-rtc-sleep-requests')>=1:
                        result=qmp('qom-set',{'path':'/machine/soc',
                                'property':'ybx-rtc-inject-deep-wakeup','value':True})
                        assert 'return' in result,result
                        injected=True
                        break
                    time.sleep(.18)
                if not injected:
                    raise RuntimeError('no RTC sleep request; no artificial wake')
                start=time.monotonic()
                frame_count=0
                while time.monotonic()-start < a.frame_limit:
                    frame_count=read(qmp,'ybx-epd-frames')
                    if frame_count>=3:
                        break
                    time.sleep(.25)
                if frame_count<3:
                    raise RuntimeError(f'only {frame_count} frames before tap')
                time.sleep(a.after_boot)
                baseline = sample(qmp,'baseline',a.out/'inkmod')
                baseline_gt911 = touch_metrics(qmp)
                taps=[]
                for i,(x,y) in enumerate(a.taps):
                    result=tap(qmp,x,y,a.press,a.settle,a.out/f'tap-{i+1:02d}')
                    taps.append(result)
                after=sample(qmp,'final',a.out/'inkmod')
                final_gt911=touch_metrics(qmp)
                verdict={'result':classify_sequence(taps),
                         'guest':'inkMOD 1.1.10', 'rtc_wake':'explicit QMP deep-wakeup',
                         'qemu_sha256':hashlib.sha256(a.qemu.read_bytes()).hexdigest(),
                         'flash_sha256':hashlib.sha256(a.flash.read_bytes()).hexdigest(),
                         'baseline':baseline, 'baseline_touch':baseline_gt911,
                         'taps':taps,'final':after,'final_touch':final_gt911,
                         'native_navigation_verified':any(t['native_ui_navigation_verified']
                                                          for t in taps)}
            finally:
                if client: client.close()
                proc.terminate()
                try:proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:proc.kill();proc.communicate()
        uart=(root/'uart0.log').read_text(errors='replace')
        verdict['uart_panic']=('Guru Meditation' in uart or 'abort() was called' in uart)
        verdict['uart_i2c_invalid_state_count']=uart.count('ESP_ERR_INVALID_STATE')
        verdict['physical_touch_verified']=False
    (a.out/'verdict.json').write_text(json.dumps(verdict,indent=2)+'\n')
    print(json.dumps({'verdict':verdict['result'],
                      'baseline':{k:verdict['baseline'].get(k) for k in
                                  ('frames','frame_sha256','nonwhite')},
                      'taps':[{'xy':t['xy'],'i2c':t['gt911_deltas'],
                               'frame':t['frame_sha_changed'],'refreshed':t['frame_refresh_after_touch'],
                               'accepted':t['native_ui_navigation_verified']}
                              for t in taps],
                      'uart_panic':verdict['uart_panic'],
                      'uart_i2c_errors':verdict['uart_i2c_invalid_state_count']},indent=2))
    return verdict


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    for prop in ('qemu','rom','flash','sd','out'):
        ap.add_argument('--'+prop,type=Path,required=True)
    ap.add_argument('--boot-limit',type=float,default=10)
    ap.add_argument('--frame-limit',type=float,default=10)
    ap.add_argument('--after-boot',type=float,default=1)
    ap.add_argument('--press',type=float,default=.45)
    ap.add_argument('--settle',type=float,default=2.4)
    ap.add_argument('--tap',dest='tap_coords',nargs=2,action='append',type=int)
    args=ap.parse_args()
    args.taps=args.tap_coords or []
    run(args)


if __name__=='__main__':
    main()
