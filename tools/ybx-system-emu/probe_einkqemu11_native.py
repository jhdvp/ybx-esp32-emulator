#!/usr/bin/env python3
"""Isolated QEMU 11.1.2 eink-qemu 0.9.2 baseline: actual original flash, HMP panel.

Run INSIDE the pinned offline Docker runtime. Copies source media to a
disposable work folder and never changes production QEMU/browser.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import time


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def png_size(path):
    raw = Path(path).read_bytes()[:24]
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Not PNG")
    return struct.unpack(">II", raw[16:24])


def hmp(sock, command):
    sock.sendall((command + "\n").encode())
    data = b""
    end = time.monotonic() + 5
    while time.monotonic() < end:
        try:
            block = sock.recv(8192)
            if not block:
                break
            data += block
            if b"(qemu)" in data:
                break
        except socket.timeout:
            break
    return data.decode(errors="replace")


def run(args):
    args.out.mkdir(parents=True, exist_ok=True)
    flash = args.out / "guest-flash.bin"
    sd = args.out / "guest-sd.img"
    assert not flash.exists() and not sd.exists(), "Never overwrite a previous guest run"
    shutil.copyfile(args.flash, flash)
    shutil.copyfile(args.sd, sd)
    before = {name: sha(path) for name, path in (
        ("source_flash", args.flash), ("source_sd", args.sd),
        ("guest_flash", flash), ("guest_sd", sd))}
    assert before["source_flash"] == before["guest_flash"]
    assert before["source_sd"] == before["guest_sd"]
    uart = args.out / "uart.log"
    errors = args.out / "qemu.stderr"
    monitor = args.out / "hmp.sock"
    cmd = [str(args.qemu), "-machine", "x4pro", "-L", str(args.rom_dir),
           "-icount", "shift=0,sleep=on", "-display", "none",
           "-drive", f"file={flash},if=mtd,format=raw",
           "-drive", f"file={sd},if=sd,format=raw",
           "-chardev", f"file,id=cdc,path={uart}", "-serial", "null",
           "-global", "driver=misc.esp32s3.usb_serial_jtag,property=chardev,value=cdc",
           "-monitor", f"unix:{monitor},server,nowait",
           "-nic", "none"]
    report = {"engine": "serialx/eink-qemu", "engine_version": "0.9.2",
              "qemu_version": "11.1.2", "machine": "x4pro",
              "qemu_sha256": sha(args.qemu), "media_before": before,
              "samples": [], "network": "disabled", "command": cmd}
    with errors.open("w") as err, subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=err
    ) as proc:
        try:
            for _ in range(100):
                if monitor.exists():
                    break
                if proc.poll() is not None:
                    raise RuntimeError(f"QEMU exited early rc={proc.returncode}: "
                                       + errors.read_text(errors="replace")[-1600:])
                time.sleep(0.1)
            if not monitor.exists():
                raise RuntimeError("HMP monitor did not open")
            with socket.socket(socket.AF_UNIX) as sock:
                sock.settimeout(0.5)
                sock.connect(str(monitor))
                time.sleep(0.15)
                sock.recv(8192)
                for i, delay in enumerate(args.sample_at):
                    time.sleep(max(0, delay - (args.sample_at[i - 1] if i else 0)))
                    frame = args.out / f"frame-{i + 1}.png"
                    reply = hmp(sock, f"screendump {frame} -f png panel")
                    row = {"seconds": delay, "hmp": reply[-400:],
                           "uart_bytes": uart.stat().st_size if uart.exists() else 0}
                    # HMP can acknowledge screendump before all PNG bytes flush.
                    for _ in range(25):
                        if frame.exists():
                            try:
                                dims = png_size(frame)
                                data = frame.read_bytes()
                                if len(data) > 128 and data.endswith(bytes.fromhex("49454e44ae426082")):
                                    row.update({"frame_sha256": sha(frame), "size": dims,
                                                "frame_file": frame.name})
                                    break
                            except ValueError:
                                pass
                        time.sleep(0.12)
                    report["samples"].append(row)
                report["hmp_info_status"] = hmp(sock, "info status")[-600:]
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    report["uart"] = uart.read_text(errors="replace")[-16000:] if uart.exists() else ""
    report["stderr_tail"] = errors.read_text(errors="replace")[-3000:]
    report["media_after"] = {"guest_flash": sha(flash), "guest_sd": sha(sd)}
    report["source_unchanged"] = (sha(args.flash) == before["source_flash"] and
                                  sha(args.sd) == before["source_sd"])
    report["frame_exists"] = any("frame_sha256" in x for x in report["samples"])
    out = args.out / "report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"source_unchanged": report["source_unchanged"],
                      "frame_exists": report["frame_exists"],
                      "samples": report["samples"],
                      "uart_tail": report["uart"][-900:],
                      "stderr_tail": report["stderr_tail"][-800:]}, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--qemu", type=Path, required=True)
    ap.add_argument("--rom-dir", type=Path, required=True)
    ap.add_argument("--flash", type=Path, required=True)
    ap.add_argument("--sd", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sample-at", type=int, nargs="+", default=[4, 10, 18])
    a = ap.parse_args()
    r = run(a)
    raise SystemExit(0 if r["source_unchanged"] and r["frame_exists"] else 1)
