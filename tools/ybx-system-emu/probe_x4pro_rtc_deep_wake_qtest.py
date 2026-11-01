#!/usr/bin/env python3
"""Real isolated QTest of explicit ESP32-S3 RTC deep-wakeup digital reset.

The input is host-injected and is NOT an emulated EXT1 GPIO wake source.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import argparse
import json
import shutil
import subprocess
from probe_x4pro_touch_injection_qtest import QTest, qmp_connect

RTC = 0x60008000
STATE0 = 0x18
RESET_CAUSE = 0x38
INT_ENA, INT_RAW, INT_ST = 0x40, 0x44, 0x48
SLEEP = 1 << 31
DEEPSLEEP_RESET = 5


def run(qemu: Path, rom: Path) -> dict:
    with TemporaryDirectory(prefix="ybx-rtc-deep-wake-") as td:
        root = Path(td)
        bios = root / "rom"
        bios.mkdir()
        shutil.copyfile(rom, bios / "esp32s3_rev0_rom.bin")
        sock = root / "qmp.sock"
        cmd = [str(qemu), "-machine", "esp32s3", "-accel", "qtest",
               "-qtest", "stdio", "-qmp", f"unix:{sock},server=on,wait=off",
               "-nographic", "-monitor", "none", "-serial", "null", "-L", str(bios)]
        with (root / "qemu.stderr").open("w") as err:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=err, text=True, bufsize=1)
            client = None
            try:
                client, reader, qmp = qmp_connect(sock)
                qt = QTest(proc)
                def read(offset):
                    return qt.rd(RTC + offset)
                def write(offset, val):
                    qt.wr(RTC + offset, val)
                def get(key):
                    ret = qmp("qom-get", {"path": "/machine/soc", "property": "ybx-rtc-" + key})
                    assert "return" in ret, ret
                    return ret["return"]
                def inject():
                    ret = qmp("qom-set", {"path": "/machine/soc",
                                          "property": "ybx-rtc-inject-deep-wakeup", "value": True})
                    assert "return" in ret, ret
                assert get("deep-wake-resets") == 0
                inject()  # Unarmed deep-sleep event is ignored.
                assert get("deep-wake-resets") == 0
                assert read(RESET_CAUSE) & 0x3f == 1  # power-on
                write(INT_ENA, 3)
                write(STATE0, SLEEP)
                assert read(INT_RAW) == 0 and read(INT_ST) == 0
                inject()  # explicit external wake, not a returning SLP_WAKEUP flag
                qt.tick(1_000_000)
                assert get("deep-wake-resets") == 1, get("deep-wake-resets")
                assert read(STATE0) & SLEEP == 0
                assert read(INT_RAW) & 3 == 0, read(INT_RAW)
                cause = read(RESET_CAUSE)
                assert (cause & 0x3f) == DEEPSLEEP_RESET, hex(cause)
                assert ((cause >> 6) & 0x3f) == DEEPSLEEP_RESET, hex(cause)
                inject()  # reset already completed and no new sleep armed
                assert get("deep-wake-resets") == 1
                return {"result": "PASS", "reset_cause": hex(cause),
                        "deep_wake_resets": get("deep-wake-resets"),
                        "int_raw": read(INT_RAW), "qtest_commands": qt.commands,
                        "auto_wake_source": False}
            finally:
                if client is not None:
                    client.close()
                proc.terminate()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                if proc.returncode not in (0, -15):
                    print((root / "qemu.stderr").read_text()[-2000:])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--qemu-bin", type=Path, required=True)
    ap.add_argument("--rom", type=Path, required=True)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    result = run(args.qemu_bin, args.rom)
    print(json.dumps(result, indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n")
