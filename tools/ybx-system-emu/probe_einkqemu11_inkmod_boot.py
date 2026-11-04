#!/usr/bin/env python3
"""Offline native inkMOD / QEMU11 X4 Pro charging-state and boot smoke probe.

Run INSIDE an isolated Linux QEMU container with read-only source images and
private writable copies. It does not connect to the LAN or touch stable 8784.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

GPIO_IN = 0x6000403C
STAT_PIN = 21


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_gpio_input(output: str) -> int:
    # QEMU HMP xp displays a physical address and a 32-bit word.
    match = re.search(r"(?i)(?:0x)?6000403c:\s*(?:0x)?([0-9a-f]{1,8})", output)
    if not match:
        raise ValueError("unrecognized HMP GPIO_IN response: " + repr(output[:160]))
    return int(match.group(1), 16)


def ppm_pixels(path: Path) -> dict:
    raw = path.read_bytes()
    match = re.match(rb"^P6\s+(\d+)\s+(\d+)\s+255\s", raw)
    if not match:
        raise ValueError("QMP screendump did not produce P6 RGB PPM")
    width, height = int(match.group(1)), int(match.group(2))
    data = raw[match.end():]
    if len(data) != width * height * 3:
        raise ValueError(f"truncated PPM: {len(data)} expected {width*height*3}")
    return {
        "width": width, "height": height,
        "sha256": hashlib.sha256(data).hexdigest(),
        "nonuniform": min(data) != max(data),
    }


class QMP:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.file = sock.makefile("rwb")
        greeting = json.loads(self.file.readline())
        if "QMP" not in greeting:
            raise RuntimeError("missing QMP greeting")
        self.seq = 0
        self.command("qmp_capabilities")

    def command(self, name: str, args: dict | None = None):
        self.seq += 1
        payload = {"execute": name, "id": self.seq}
        if args is not None:
            payload["arguments"] = args
        self.file.write((json.dumps(payload) + "\n").encode())
        self.file.flush()
        while True:
            reply = json.loads(self.file.readline())
            if reply.get("id") != self.seq:
                continue
            if "error" in reply:
                raise RuntimeError(f"QMP {name}: {reply['error']}")
            return reply.get("return")

    def close(self):
        self.file.close()
        self.sock.close()


def connect_qmp(path: Path, deadline: float) -> QMP:
    while time.monotonic() < deadline:
        try:
            sock = socket.socket(socket.AF_UNIX)
            sock.settimeout(10)
            sock.connect(str(path))
            return QMP(sock)
        except (OSError, TimeoutError):
            time.sleep(0.1)
    raise TimeoutError("QMP socket not created")


def run(args) -> dict:
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite output: {args.output}")
    if args.flash.stat().st_size != 16 * 1024 * 1024:
        raise ValueError("expected original full 16MiB X4 Pro flash")
    args.output.mkdir(parents=True)
    flash = args.output / "guest-flash.bin"
    sd = args.output / "guest-sd.img"
    flash.write_bytes(args.flash.read_bytes())
    sd.write_bytes(args.sd.read_bytes())
    baseline = {"flash": sha256(args.flash), "sd": sha256(args.sd)}
    qmp_sock = args.output / "qmp.sock"
    uart = args.output / "cdc-uart.log"
    cmd = [
        str(args.qemu), "-machine", "x4pro", "-L", str(args.rom_dir),
        "-icount", "shift=0,sleep=on", "-display", "none",
        "-no-reboot", "-S", "-nic", "none", "-monitor", "none",
        "-drive", f"file={flash},if=mtd,format=raw",
        "-drive", f"file={sd},if=sd,format=raw",
        "-chardev", f"file,id=cdc,path={uart}",
        "-serial", f"file:{args.output / 'uart0.log'}",
        "-global", "driver=misc.esp32s3.usb_serial_jtag,property=chardev,value=cdc",
        "-qmp", f"unix:{qmp_sock},server=on,wait=off",
    ]
    report = {"reference": "serialx/eink-qemu 0.9.2", "qemu_sha256": sha256(args.qemu),
              "source_media_sha256": baseline, "command": cmd}
    with (args.output / "qemu.stderr").open("wb") as stderr:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr)
        qmp = None
        try:
            qmp = connect_qmp(qmp_sock, time.monotonic() + 12)
            report["charging_default"] = qmp.command("qom-get", {
                "path": "/machine/x4pro-keys", "property": "charging"})
            def input_pin():
                result = qmp.command("human-monitor-command", {
                    "command-line": f"xp /1wx {GPIO_IN:#x}"})
                return (parse_gpio_input(result) >> STAT_PIN) & 1
            report["gpio21_initial"] = input_pin()
            qmp.command("qom-set", {"path": "/machine/x4pro-keys",
                                     "property": "charging", "value": True})
            report["gpio21_charging"] = input_pin()
            qmp.command("qom-set", {"path": "/machine/x4pro-keys",
                                     "property": "charging", "value": False})
            report["gpio21_restored"] = input_pin()
            report["charger_model_ok"] = (
                report["charging_default"] is False and
                [report["gpio21_initial"], report["gpio21_charging"],
                 report["gpio21_restored"]] == [0, 1, 0])
            if not report["charger_model_ok"]:
                raise RuntimeError("charger status does not drive GPIO21 LOW/HIGH/LOW")
            qmp.command("cont")
            frames = []
            start = time.monotonic()
            for when in args.sample_seconds:
                while time.monotonic() - start < when and proc.poll() is None:
                    time.sleep(0.2)
                if proc.poll() is not None:
                    break
                dest = args.output / f"frame-{when}.ppm"
                qmp.command("screendump", {"filename": str(dest), "format": "ppm"})
                frames.append({"seconds": when, **ppm_pixels(dest)})
                if args.tap_after is not None and when == args.tap_after:
                    if not (0 <= args.tap_x < 480 and 0 <= args.tap_y < 800):
                        raise ValueError("tap outside 480x800")
                    events = [
                        {"type": "abs", "data": {"axis": "x", "value": round(32767 * args.tap_x / 479)}},
                        {"type": "abs", "data": {"axis": "y", "value": round(32767 * args.tap_y / 799)}},
                        {"type": "btn", "data": {"button": "left", "down": True}},
                    ]
                    qmp.command("input-send-event", {"events": events})
                    try:
                        time.sleep(0.20)
                    finally:
                        qmp.command("input-send-event", {"events": [
                            {"type": "btn", "data": {"button": "left", "down": False}}]})
                    report["touch_injected"] = {"x": args.tap_x, "y": args.tap_y,
                                                "after_seconds": when, "before_sha": frames[-1]["sha256"]}
            report["frames"] = frames
            if report.get("touch_injected"):
                report["touch_frame_changed"] = any(
                    f["seconds"] > report["touch_injected"]["after_seconds"] and
                    f["sha256"] != report["touch_injected"]["before_sha"] for f in frames)
            report["guest_uart_excerpt"] = uart.read_text(errors="replace")[-4000:] if uart.exists() else ""
            report["panic"] = "Guru Meditation Error" in report["guest_uart_excerpt"]
            report["frame_ok"] = any(
                f["width"] == 480 and f["height"] == 800 and f["nonuniform"]
                for f in frames)
        finally:
            if qmp is not None:
                qmp.close()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
    report["original_media_unchanged"] = (
        sha256(args.flash) == baseline["flash"] and sha256(args.sd) == baseline["sd"])
    report["pass"] = bool(
        report.get("charger_model_ok") and not report.get("panic", True) and
        report.get("frame_ok") and report["original_media_unchanged"] and
        (not report.get("touch_injected") or report.get("touch_frame_changed")))
    (args.output / "report.json").write_text(json.dumps(report, indent=2))
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ("qemu", "rom-dir", "flash", "sd", "output"):
        ap.add_argument("--" + name, type=Path, required=True)
    ap.add_argument("--sample-seconds", type=int, nargs="+", default=[5, 15, 30])
    ap.add_argument("--tap-after", type=int, default=None,
                    help="At which sample second to inject real QMP GT911 touch")
    ap.add_argument("--tap-x", type=int, default=160)
    ap.add_argument("--tap-y", type=int, default=342)
    opts = ap.parse_args()
    try:
        result = run(opts)
    except Exception as exc:
        print(f"PROBE ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({k: result.get(k) for k in (
        "charger_model_ok", "panic", "frame_ok", "original_media_unchanged", "pass", "frames")}, indent=2))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
