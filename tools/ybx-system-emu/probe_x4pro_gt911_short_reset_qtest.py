#!/usr/bin/env python3
"""Real QTest regression: 30us GPIO4 glitch must not latch GT911 offline.

Only a >=2ms reset pulse should discard the previous ready state and
schedule 50ms startup. A low reset line still NACKs while asserted.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from probe_x4pro_touch_injection_qtest import QTest, qmp_connect

GPIO = 0x60004000
ADDR = 0x5d


def run(qemu: Path, rom: Path) -> dict:
    with TemporaryDirectory(prefix='ybx-gt911-short-reset-') as temp:
        root = Path(temp)
        romdir = root / 'rom'
        romdir.mkdir()
        (romdir / 'esp32s3_rev0_rom.bin').write_bytes(rom.read_bytes())
        sock = root / 'qmp.sock'
        cmd = [str(qemu), '-machine', 'esp32s3,ybx-x4pro-i2c=on,ybx-x4pro-gt911=on',
               '-accel', 'qtest', '-qtest', 'stdio', '-qmp',
               f'unix:{sock},server=on,wait=off', '-nographic', '-monitor', 'none',
               '-serial', 'null', '-L', str(romdir)]
        with (root / 'stderr.log').open('w') as stderr:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=stderr, text=True, bufsize=1)
            client = None
            try:
                client, reader, qmp = qmp_connect(sock)
                qt = QTest(proc)
                def metric(key):
                    response = qmp('qom-get', {'path': '/machine/soc',
                                               'property': 'ybx-gt911-' + key})
                    assert 'return' in response, response
                    return response['return']
                # Board: GPIO1 master rail high, GPIO2 active-low touch rail,
                # GPIO4 reset (push-pull). Register +0x08 is W1TS, +0x0c W1TC.
                qt.wr(GPIO + 0x24, 0x416)
                assert qt.i2c_send(ADDR, [0x81, 0x40]) == 0x400
                qt.wr(GPIO + 0x08, 1 << 1)
                qt.tick(3_000_000)
                qt.wr(GPIO + 0x08, 1 << 4)
                qt.tick(60_000_000)
                assert metric('ready') is True
                assert metric('qualified-reset-rises') == 1
                assert qt.i2c_read(ADDR, 0x8140, 3) == [ord('9'), ord('1'), ord('1')]
                # Preserve a queued real (synthetic) touch frame through a
                # sub-minimum pulse; this checks more than one ACK.
                for key, value in [('x', 120), ('y', 640), ('state', 1)]:
                    response = qmp('qom-set', {'path': '/machine/soc',
                                               'property': 'ybx-touch-' + key,
                                               'value': value})
                    assert 'return' in response, response
                assert qt.i2c_read(ADDR, 0x814e, 1) == [0x81]
                qt.wr(GPIO + 0x0c, 1 << 4)  # GPIO4 low
                qt.tick(30_000)
                assert qt.i2c_send(ADDR, [0x81, 0x40]) == 0x400
                qt.wr(GPIO + 0x08, 1 << 4)  # GPIO4 high, pulse is invalid
                assert metric('short-reset-rises') == 1
                assert metric('last-reset-low-us') == 30
                assert metric('ready') is True
                assert qt.i2c_read(ADDR, 0x814e, 1) == [0x81]
                assert metric('gate-nacks') >= 2  # while power off / reset low
                # A real >=2ms pulse *does* reset the device and mandates
                # another 50ms readiness delay.
                qt.wr(GPIO + 0x0c, 1 << 4)
                qt.tick(3_000_000)
                qt.wr(GPIO + 0x08, 1 << 4)
                assert metric('qualified-reset-rises') == 2
                assert metric('ready') is False
                assert qt.i2c_send(ADDR, [0x81, 0x40]) == 0x400
                qt.tick(60_000_000)
                assert metric('ready') is True
                assert qt.i2c_read(ADDR, 0x814e, 1) == [0]
                return {'result': 'PASS', 'short_pulse_us': 30,
                        'short_count': metric('short-reset-rises'),
                        'valid_resets': metric('qualified-reset-rises'),
                        'qtest_commands': qt.commands,
                        'device_address': hex(ADDR)}
            finally:
                if client is not None:
                    client.close()
                proc.terminate()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu-bin', type=Path, required=True)
    parser.add_argument('--rom', type=Path, required=True)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    result = run(args.qemu_bin, args.rom)
    print(json.dumps(result, indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + '\n')
