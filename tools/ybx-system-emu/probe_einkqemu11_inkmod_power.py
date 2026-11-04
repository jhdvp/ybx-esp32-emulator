#!/usr/bin/env python3
"""Isolated original inkMOD native Power -> RTC deep sleep -> EXT1 wake probe."""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from probe_einkqemu11_inkmod_boot import QMP, connect_qmp, ppm_pixels, sha256

KEY_PATH = "/machine/x4pro-keys"


def launch(a: argparse.Namespace) -> dict:
    if a.output.exists():
        raise FileExistsError(f"refusing output overwrite: {a.output}")
    a.output.mkdir(parents=True)
    flash, sd = a.output / "flash.bin", a.output / "sd.img"
    shutil.copyfile(a.flash, flash)
    shutil.copyfile(a.sd, sd)
    original = {"flash": sha256(a.flash), "sd": sha256(a.sd)}
    sock = a.output / "qmp.sock"
    uart = a.output / "usb-uart.log"
    cmd = [
        str(a.qemu), "-machine", "x4pro", "-L", str(a.rom_dir),
        "-icount", "shift=0,sleep=on", "-display", "none", "-nic", "none",
        "-drive", f"file={flash},if=mtd,format=raw",
        "-drive", f"file={sd},if=sd,format=raw",
        "-chardev", f"file,id=cdc,path={uart}", "-serial", "null",
        "-global", "driver=misc.esp32s3.usb_serial_jtag,property=chardev,value=cdc",
        "-qmp", f"unix:{sock},server=on,wait=off",
    ]
    report = {"qemu_sha256": sha256(a.qemu), "original": original}
    with (a.output / "qemu.stderr").open("wb") as error_log:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=error_log)
        client = None
        try:
            client = connect_qmp(sock, time.monotonic() + 10)
            status = lambda: client.command("query-status")["status"]
            set_power = lambda pressed: client.command("qom-set", {
                "path": KEY_PATH, "property": "power", "value": pressed
            })
            # A real guest Home render needs time to finish UC8279 update and SD init.
            time.sleep(a.boot_seconds)
            report["status_at_home"] = status()
            home = a.output / "home.ppm"
            client.command("screendump", {"filename": str(home), "format": "ppm"})
            report["home"] = ppm_pixels(home)
            if report["status_at_home"] != "running" or not report["home"]["nonuniform"]:
                raise RuntimeError("Home was not ready for power test")
            # Keep key asserted while firmware handles the Power event.
            set_power(True)
            report["power_down_observed"] = client.command("qom-get", {
                "path": KEY_PATH, "property": "power"})
            try:
                time.sleep(a.press_seconds)
            finally:
                set_power(False)
            report["power_up_observed"] = client.command("qom-get", {
                "path": KEY_PATH, "property": "power"})
            states = []
            limit = time.monotonic() + a.sleep_deadline
            while time.monotonic() < limit and proc.poll() is None:
                state = status()
                if not states or states[-1] != state:
                    states.append(state)
                if state == "suspended":
                    break
                time.sleep(0.22)
            report["prewake_states"] = states
            report["suspended"] = states and states[-1] == "suspended"
            if not report["suspended"]:
                failure_frame = a.output / "failed-power.ppm"
                client.command("screendump", {"filename": str(failure_frame), "format": "ppm"})
                report["failed_power_frame"] = ppm_pixels(failure_frame)
                report["error"] = "RTC deep-sleep SUSPENDED not reached"
                (a.output / "diagnostic.json").write_text(json.dumps(report, indent=2))
                raise RuntimeError("RTC deep-sleep SUSPENDED not reached")
            # QEMU's RTCCNTL wake callback must reset the digital core,
            # not only change the QOM power property.
            set_power(True)
            try:
                state_after_wake = []
                deadline = time.monotonic() + a.wake_deadline
                while time.monotonic() < deadline and proc.poll() is None:
                    state = status()
                    if not state_after_wake or state_after_wake[-1] != state:
                        state_after_wake.append(state)
                    if state == "running":
                        break
                    time.sleep(0.2)
                report["postwake_states"] = state_after_wake
            finally:
                time.sleep(a.wake_hold_seconds)
                set_power(False)
            report["resumed"] = bool(report["postwake_states"] and report["postwake_states"][-1] == "running")
            if not report["resumed"]:
                raise RuntimeError("RTC wake did not resume digital SoC")
            time.sleep(a.postwake_seconds)
            report["final_state"] = status()
            after = a.output / "after-wake.ppm"
            client.command("screendump", {"filename": str(after), "format": "ppm"})
            report["after_wake"] = ppm_pixels(after)
        finally:
            if client:
                client.close()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
    guest_uart = uart.read_text(errors="replace") if uart.exists() else ""
    report["panics"] = guest_uart.count("Guru Meditation Error")
    report["source_media_unchanged"] = sha256(a.flash) == original["flash"] and sha256(a.sd) == original["sd"]
    report["pass"] = bool(
        report.get("suspended") and report.get("resumed") and
        report.get("after_wake", {}).get("nonuniform") and
        report["panics"] == 0 and report["source_media_unchanged"])
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    for field in ("qemu", "rom-dir", "flash", "sd", "output"):
        ap.add_argument("--" + field, type=Path, required=True)
    ap.add_argument("--boot-seconds", type=int, default=55)
    ap.add_argument("--press-seconds", type=float, default=1.5)
    ap.add_argument("--sleep-deadline", type=int, default=45)
    ap.add_argument("--wake-deadline", type=int, default=15)
    ap.add_argument("--wake-hold-seconds", type=float, default=0.85)
    ap.add_argument("--postwake-seconds", type=int, default=25)
    a = ap.parse_args()
    try:
        report = launch(a)
        code = 0 if report["pass"] else 1
    except Exception as e:
        report = {"error": f"{type(e).__name__}: {e}", "pass": False}
        code = 2
    if a.output.exists():
        (a.output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
