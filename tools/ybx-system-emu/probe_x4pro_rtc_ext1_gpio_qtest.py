#!/usr/bin/env python3
"""Real ESP32-S3 QTest: guest-configured EXT1 levels trigger DSLEEP reset.

Explicit test pad levels simulate an *external electrical input*, not a
software command to wake. The RTC controller itself must enforce the guest
mask, EXT1 enable, ANY_HIGH/ALL_LOW condition and armed sleep state.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import argparse
import json
import shutil
import subprocess
from probe_x4pro_touch_injection_qtest import QTest, qmp_connect

RTC = 0x60008000
STATE0, WAKEUP, RESET_CAUSE = 0x18, 0x3c, 0x38
EXT1_CONF, EXT1_SEL, EXT1_STATUS = 0x64, 0xe0, 0xe4
SLEEP = 1 << 31
EXT1_WAKE = 1 << 16
ANY_HIGH = 1 << 31
CLEAR_STATUS = 1 << 22
DEEPSLEEP_RESET = 5


def run(qemu: Path, rom: Path) -> dict:
    with TemporaryDirectory(prefix='ybx-ext1-qtest-') as tmp:
        root = Path(tmp)
        bios = root / 'rom'; bios.mkdir()
        shutil.copyfile(rom, bios / 'esp32s3_rev0_rom.bin')
        sock = root / 'qmp.sock'
        cmd = [str(qemu), '-machine', 'esp32s3', '-accel', 'qtest',
               '-qtest', 'stdio', '-qmp', f'unix:{sock},server=on,wait=off',
               '-nographic', '-monitor', 'none', '-serial', 'null', '-L', str(bios)]
        with (root / 'stderr.log').open('w') as stderr:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=stderr,
                                    text=True, bufsize=1)
            client = None
            try:
                client, reader, qmp = qmp_connect(sock)
                qt = QTest(proc)
                def rd(off): return qt.rd(RTC + off)
                def wr(off, val): qt.wr(RTC + off, val)
                def get(prop):
                    reply = qmp('qom-get', {'path': '/machine/soc',
                                            'property': 'ybx-rtc-' + prop})
                    assert 'return' in reply, reply
                    return reply['return']
                def level(pin, high, expect_error=False):
                    a = qmp('qom-set', {'path': '/machine/soc',
                                      'property': 'ybx-rtc-ext1-test-pin',
                                      'value': pin})
                    assert 'return' in a, a
                    b = qmp('qom-set', {'path': '/machine/soc',
                                      'property': 'ybx-rtc-ext1-test-level',
                                      'value': high})
                    assert ('error' in b) if expect_error else ('return' in b), b
                    return b
                assert rd(EXT1_SEL) == rd(EXT1_STATUS) == rd(EXT1_CONF) == 0
                assert get('ext1-wake-events') == get('ext1-pad-valid') == 0
                # External levels before any guest programming cannot wake.
                level(3, False)
                level(7, False)
                assert get('ext1-wake-events') == 0
                wr(EXT1_SEL, (1 << 3) | (1 << 7))
                wr(EXT1_CONF, 0)  # ALL_LOW on both selected pads
                wr(STATE0, SLEEP)
                assert get('ext1-wake-events') == 0  # no EXT1 enable
                wr(STATE0, 0)
                wr(WAKEUP, EXT1_WAKE)
                # Incorrect pin or mode cannot wake.
                level(8, False)
                wr(EXT1_CONF, ANY_HIGH)
                wr(STATE0, SLEEP)
                assert get('ext1-wake-events') == 0
                assert rd(STATE0) & SLEEP
                level(3, False)  # known selected low, but ANY_HIGH
                assert get('ext1-wake-events') == 0
                level(7, True)  # selected high -> deep reset
                qt.tick(1_000_000)
                assert get('ext1-wake-events') == 1
                assert rd(STATE0) & SLEEP == 0
                assert rd(EXT1_STATUS) == (1 << 7)
                cause = rd(RESET_CAUSE)
                assert cause & 0x3f == DEEPSLEEP_RESET, hex(cause)
                assert (cause >> 6) & 0x3f == DEEPSLEEP_RESET, hex(cause)
                assert rd(EXT1_SEL) == (1 << 3) | (1 << 7)
                # A second external level change while not in sleep does NOT
                # reset. W1C clear must not discard the programmed pad mask.
                level(7, False)
                assert get('ext1-wake-events') == 1
                wr(EXT1_SEL, (1 << 3) | (1 << 7) | CLEAR_STATUS)
                assert rd(EXT1_STATUS) == 0
                assert rd(EXT1_SEL) == (1 << 3) | (1 << 7)
                # Two-pin ALL_LOW: one unobserved or high pad vetoes wake.
                wr(EXT1_CONF, 0)
                level(3, False)
                level(7, True)
                wr(STATE0, SLEEP)
                assert get('ext1-wake-events') == 1
                level(7, False)
                qt.tick(1_000_000)
                assert get('ext1-wake-events') == 2
                assert rd(EXT1_STATUS) == ((1 << 3) | (1 << 7))
                # Wake does not work with EXT1 disabled or no selected pins.
                wr(STATE0, 0)
                wr(WAKEUP, 0)
                wr(STATE0, SLEEP)
                level(3, True)
                level(3, False)
                assert get('ext1-wake-events') == 2
                wr(STATE0, 0)
                wr(WAKEUP, EXT1_WAKE)
                wr(EXT1_SEL, 0)
                wr(STATE0, SLEEP)
                level(3, False)
                assert get('ext1-wake-events') == 2
                # Reject invalid RTCIO index and preserve wake counter.
                level(22, True, expect_error=True)
                assert get('ext1-wake-events') == 2
                return {'result': 'PASS', 'ext1_wake_events': 2,
                        'cause': hex(cause), 'qtest_commands': qt.commands,
                        'hardware_gpio_link_verified': False,
                        'qmp_test_levels_are_electrical_stimuli': True}
            finally:
                if client is not None: client.close()
                proc.terminate()
                try: proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.communicate()


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--qemu-bin', type=Path, required=True)
    ap.add_argument('--rom', type=Path, required=True)
    ap.add_argument('--out', type=Path)
    args = ap.parse_args()
    report = run(args.qemu_bin, args.rom)
    print(json.dumps(report, indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + '\n')
