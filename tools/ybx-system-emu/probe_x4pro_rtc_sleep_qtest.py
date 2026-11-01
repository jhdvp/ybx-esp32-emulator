#!/usr/bin/env python3
"""Real QTest/QMP acceptance of the experimental S3 RTC sleep handshake.

No guest firmware or real wake source is assumed: only a host-injected event
may set INT_RAW. Run on a disposable QEMU copy, never the shared browser.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from probe_x4pro_touch_injection_qtest import QTest, qmp_connect

RTC = 0x60008000
STATE0, WAKEUP, ENA, RAW, ST, CLR, REJECT_CONF = (0x18, 0x3c, 0x40, 0x44, 0x48, 0x4c, 0x68)
SLEEP = 1 << 31


def run(qemu: Path, rom: Path) -> dict:
    with TemporaryDirectory(prefix="ybx-rtc-sleep-qtest-") as td:
        root = Path(td)
        bios = root / "rom"
        bios.mkdir()
        shutil.copyfile(rom, bios / "esp32s3_rev0_rom.bin")
        sock = root / "qmp.sock"
        cmd = [str(qemu), "-machine", "esp32s3", "-accel", "qtest",
               "-qtest", "stdio", "-qmp", f"unix:{sock},server=on,wait=off",
               "-nographic", "-monitor", "none", "-serial", "null", "-L", str(bios)]
        with (root / "qemu.stderr").open("w") as log:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=log, text=True, bufsize=1)
            client = None
            try:
                client, reader, qmp = qmp_connect(sock)
                qt = QTest(proc)
                trace = []
                def rd(offset):
                    return qt.rd(RTC + offset)
                def wr(offset, value):
                    qt.wr(RTC + offset, value)
                def get(prop):
                    response = qmp("qom-get", {"path": "/machine/soc",
                                               "property": "ybx-rtc-" + prop})
                    assert "return" in response, response
                    return response["return"]
                def inject(kind):
                    response = qmp("qom-set", {"path": "/machine/soc",
                                               "property": "ybx-rtc-inject-" + kind,
                                               "value": True})
                    assert "return" in response, response
                assert rd(RAW) == rd(ST) == rd(ENA) == 0
                assert rd(CLR) == 0
                inject("wakeup")  # no sleep request: no spontaneous wake
                assert get("int-raw") == 0
                wr(ENA, 0x3)
                assert rd(ST) == 0  # enabled but no event
                wr(WAKEUP, 2 << 15)  # EXT1 selected by guest
                wr(REJECT_CONF, 0x30000000)
                assert rd(WAKEUP) == 2 << 15
                assert rd(REJECT_CONF) == 0x30000000
                wr(STATE0, SLEEP)
                assert rd(STATE0) & SLEEP
                assert get("sleep-requests") == 1
                assert rd(RAW) == 0, "wakeup fabricated just by starting sleep"
                inject("wakeup")
                assert rd(RAW) & 1 and rd(ST) & 1
                assert rd(STATE0) & (1 << 29)
                assert rd(STATE0) & SLEEP == 0
                assert get("injected-wakeups") == 1
                trace.append({"event": "wakeup", "raw": rd(RAW), "state0": rd(STATE0)})
                wr(CLR, 1)
                assert rd(RAW) == 0 and rd(ST) == 0
                assert rd(CLR) == 0
                inject("reject")  # already awake: must not create an IRQ
                assert rd(RAW) == 0
                wr(STATE0, SLEEP)
                inject("reject")
                assert rd(RAW) == 2 and rd(ST) == 2
                assert rd(STATE0) & (1 << 30)
                assert get("injected-rejects") == 1
                trace.append({"event": "reject", "raw": rd(RAW), "state0": rd(STATE0)})
                wr(ENA, 0)
                assert rd(RAW) == 2 and rd(ST) == 0, "RAW is not interrupt mask"
                wr(CLR, 2)
                assert rd(RAW) == 0
                # Digital reset is NOT a RTC-domain cold reset in this
                # QEMU machine. Observe retention rather than inventing a
                # full RTC reset that the machine does not send.
                before_reset = {"state0": rd(STATE0),
                                "sleep_requests": get("sleep-requests"),
                                "wakes": get("injected-wakeups"),
                                "rejects": get("injected-rejects")}
                response = qmp("system_reset")
                assert "return" in response, response
                after_reset = {"state0": rd(STATE0),
                               "sleep_requests": get("sleep-requests"),
                               "wakes": get("injected-wakeups"),
                               "rejects": get("injected-rejects")}
                assert after_reset == before_reset, (before_reset, after_reset)
                assert rd(RAW) == 0 and rd(ENA) == 0
                return {"result": "PASS", "qtest_commands": qt.commands,
                        "events": trace, "rtc_domain_retained_on_system_reset": True,
                        "automatic_wakeup": False}
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


def main():
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


if __name__ == "__main__":
    main()
