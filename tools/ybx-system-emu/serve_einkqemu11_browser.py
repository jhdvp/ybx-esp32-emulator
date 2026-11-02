#!/usr/bin/env python3
"""YBX browser compatibility bridge for verified eink-qemu 0.9.2 / QEMU 11.1.2.

Separately launched, loopback-only: uses native x4pro/UC8279/GT911/USB-CDC
and its QMP input-send-event. Does not modify the established 8784 instance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import zlib

from serve_x4pro_browser import PAGE as LEGACY_PAGE

# Keep the existing YBX UI while replacing every legacy-hardware disclaimer.
# The old renderer was SSD1677 / no Wi-Fi; native eink-qemu11 uses UC8279
# and has a real ESP32-S3 MAC connected to a virtual AP when NAT is enabled.
PAGE = (LEGACY_PAGE
    .replace("Real SSD1677 framebuffer, YBX X4 Pro",
             "Native UC8279 framebuffer, Xteink X4 Pro")
    .replace("QEMU ESP32-S3 · active ROM shown below",
             "eink-qemu 0.9.2 · QEMU 11.1.2")
    .replace("real guest-written SSD1677 480×800 RAM",
             "real guest-rendered UC8279 480×800 panel")
    .replace("<p><strong>Wi-Fi radio is not emulated.</strong> Join a Network,\n"
             "Calibre Wireless and Create Hotspot cannot connect from this factory guest.\n"
             "Attempting network setup may stop its interface responding; use\n"
             "<strong>Restart virtual reader</strong> to reboot the virtual device.\n"
             "If the emulator process itself stopped, the same button starts a fresh guest.</p>",
             "<p><strong>Native Wi-Fi MAC available.</strong> When enabled by the "
             "preview launcher, the firmware can join the virtual open AP "
             "PICSimLabWifi through QEMU NAT. This is not a real WPA network. "
             "Only a loopback-only experimental preview; production 8784 is separate.</p>")
    .replace("Some features (Wi-Fi, hardware timing,\n"
             "real E-Ink waveform and other panel variants) remain unimplemented.",
             "Hardware waveforms, all RF modes and physical controls are not "
             "fully modeled; see the 0108 acceptance report.")
    .replace("The ROM\nfolder is read-only for this server; loading a ROM restarts the guest and\n"
             "the page only claims what the frame counter actually shows.",
             "ROM comes from the verified upstream release; this preview does "
             "not support arbitrary ROM replacement.")
    .replace("Experimental X4 Pro GPIO3 / RTC EXT1. Hold the virtual key;",
             "Native X4 Pro board GPIO3 Power key. Hold the virtual key;"))

W, H = 480, 800
KNOWN_BINARY_SHA = "8fce2150c091ceb7b644ac7603169620e9fc171ebaa37bed8aece2a457dcbd6a"


def file_sha(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def decode_png_rgb(png):
    """Decode QEMU's 8-bit RGB panel capture without external dependencies."""
    if png[:8] != bytes.fromhex("89504e470d0a1a0a"):
        raise ValueError("expected PNG panel")
    offset = 8
    stream = []
    width = height = depth = color = None
    while offset + 12 <= len(png):
        size = int.from_bytes(png[offset:offset + 4], "big")
        tag = png[offset + 4:offset + 8]
        chunk = png[offset + 8:offset + 8 + size]
        if len(chunk) != size:
            raise ValueError("incomplete PNG")
        if tag == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", chunk[:10])
        elif tag == b"IDAT":
            stream.append(chunk)
        elif tag == b"IEND":
            break
        offset += size + 12
    if (tag != b"IEND" or (width, height, depth, color) != (W, H, 8, 2)):
        raise ValueError("unsupported / incomplete panel PNG")
    filtered = zlib.decompress(b"".join(stream))
    stride = width * 3
    if len(filtered) != height * (stride + 1):
        raise ValueError("invalid RGB scanline size")
    previous = bytearray(stride)
    pixels = bytearray()
    pos = 0
    for _ in range(height):
        kind = filtered[pos]
        row = bytearray(filtered[pos + 1:pos + 1 + stride])
        pos += stride + 1
        if kind not in range(5):
            raise ValueError("unknown PNG filter")
        for i in range(stride):
            a = row[i - 3] if i >= 3 else 0
            b = previous[i]
            c = previous[i - 3] if i >= 3 else 0
            if kind == 1:
                predictor = a
            elif kind == 2:
                predictor = b
            elif kind == 3:
                predictor = (a + b) // 2
            elif kind == 4:
                p = a + b - c
                delta = (abs(p - a), abs(p - b), abs(p - c))
                predictor = (a, b, c)[delta.index(min(delta))]
            else:
                predictor = 0
            row[i] = (row[i] + predictor) & 255
        pixels.extend(row)
        previous = row
    return bytes(pixels)


def choose_media(flash_source, sd_source, target):
    target.mkdir(parents=True, exist_ok=True)
    flash, sd = target / "flash.bin", target / "sd.img"
    if flash.is_symlink() or sd.is_symlink():
        raise RuntimeError("guest media symlinks are prohibited")
    if flash.exists() != sd.exists():
        raise RuntimeError("incomplete persisted flash+SD; refusing to reset data")
    if not flash.exists():
        shutil.copyfile(flash_source, flash)
        shutil.copyfile(sd_source, sd)
    for p, source in ((flash, flash_source), (sd, sd_source)):
        if not p.is_file() or p.stat().st_size != source.stat().st_size:
            raise RuntimeError(f"guest media wrong size: {p}")
    return flash, sd


class Qemu11:
    def __init__(self, args):
        self.args = args
        self.lock = threading.RLock()
        self.proc = None
        self.power_pressed = False
        self.power_generation = 0
        self.power_timer = None
        self.persistent_dir = Path(args.work_dir)
        self.persistent_dir.mkdir(parents=True, exist_ok=True)
        # QMP Unix sockets and screenshots live in Linux /tmp, never on a
        # macOS Docker bind mount; guest flash/SD alone are persistent.
        self.work = Path(tempfile.mkdtemp(prefix="ybx-einkqemu11-"))
        self.flash, self.sd = choose_media(args.flash, args.sd, self.persistent_dir)
        self.media_initial = {"flash": file_sha(self.flash), "sd": file_sha(self.sd)}
        self.rom = args.rom_dir / "esp32s3_rev0_rom.bin"
        if not self.rom.is_file():
            raise RuntimeError("missing ESP32-S3 ROM from eink-qemu release")
        if file_sha(args.qemu) != KNOWN_BINARY_SHA:
            raise RuntimeError("not the audited eink-qemu 0.9.2 arm64 binary")
        self.fw_name = args.flash.name
        self.fw_sha = file_sha(args.flash)
        self.sidecar = self.persistent_dir / "selected-firmware.json"
        if self.sidecar.exists():
            saved = json.loads(self.sidecar.read_text())
            name = saved.get("name", "")
            if (not isinstance(name, str) or Path(name).name != name
                    or not (args.firmware_dir / name).is_file()):
                raise RuntimeError("invalid persisted firmware selection")
            self.fw_name = name
            self.fw_sha = file_sha(args.firmware_dir / name)
        self.png = b""
        self.frame_sha = ""
        self.nonwhite = 0
        self.frames = 0
        self.last_frame_poll = 0.0
        self.boot_time = time.monotonic()
        self.start()

    def start(self):
        with self.lock:
            self.stop()
            self.png = b""
            self.frame_sha = ""
            self.nonwhite = 0
            self.frames = 0
            self.last_frame_poll = 0.0
            self.boot_time = time.monotonic()
            self.sock = self.work / "qmp.sock"
            self.sock.unlink(missing_ok=True)
            self.uart = self.work / "usb-cdc.log"
            self.uart.unlink(missing_ok=True)
            self.err = self.work / "qemu.stderr"
            cmd = [str(self.args.qemu), "-machine", "x4pro",
                   "-L", str(self.args.rom_dir),
                   "-icount", "shift=0,sleep=on",
                   "-display", "none", "-serial", "null",
                   "-drive", f"file={self.flash},if=mtd,format=raw",
                   "-drive", f"file={self.sd},if=sd,format=raw",
                   "-chardev", f"file,id=cdc,path={self.uart}",
                   "-global", "driver=misc.esp32s3.usb_serial_jtag,property=chardev,value=cdc",
                   "-qmp", f"unix:{self.sock},server=on,wait=off",
                   "-nic", "user,model=esp32_wifi" if self.args.wifi else "none"]
            self.error_handle = self.err.open("w")
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                         stderr=self.error_handle)
            for _ in range(90):
                if self.sock.exists():
                    break
                if self.proc.poll() is not None:
                    raise RuntimeError("QEMU11 exited: " + self.err.read_text()[-1500:])
                time.sleep(0.1)
            if not self.sock.exists():
                raise RuntimeError("QEMU11 QMP socket missing")
            self.qmp("query-status")

    def stop(self):
        if self.power_timer is not None:
            self.power_timer.cancel()
            self.power_timer = None
        self.power_generation += 1
        self.power_pressed = False
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
            self.proc = None
            self.error_handle.close()

    def qmp(self, command, arguments=None):
        with self.lock:
            with socket.socket(socket.AF_UNIX) as sock:
                sock.settimeout(8)
                sock.connect(str(self.sock))
                with sock.makefile("rb") as reader:
                    hello = json.loads(reader.readline())
                    if "QMP" not in hello:
                        raise RuntimeError("missing QMP greeting")
                    def exchange(cmd, opts=None):
                        request = {"execute": cmd}
                        if opts is not None:
                            request["arguments"] = opts
                        sock.sendall((json.dumps(request) + "\n").encode())
                        for _ in range(40):
                            result = json.loads(reader.readline())
                            if "event" in result:
                                continue
                            if "error" in result:
                                raise RuntimeError(f"QMP {cmd}: {result['error']}")
                            return result["return"]
                        raise RuntimeError("QMP lost response")
                    exchange("qmp_capabilities")
                    return exchange(command, arguments)

    def capture(self):
        with self.lock:
            if time.monotonic() - self.last_frame_poll < 0.55 and self.png:
                return
            target = self.work / "next-panel.png"
            target.unlink(missing_ok=True)
            self.qmp("human-monitor-command",
                     {"command-line": f"screendump {target} -f png panel"})
            raw = None
            for _ in range(25):
                if target.is_file():
                    data = target.read_bytes()
                    if data.endswith(bytes.fromhex("49454e44ae426082")):
                        raw = data
                        break
                time.sleep(0.10)
            if raw is None:
                raise RuntimeError("QEMU11 panel screenshot incomplete")
            rgb = decode_png_rgb(raw)
            checksum = hashlib.sha256(rgb).hexdigest()
            if checksum != self.frame_sha:
                self.frame_sha = checksum
                self.png = raw
                self.nonwhite = sum(b != 255 for b in rgb)
                if self.nonwhite:
                    self.frames += 1
            self.last_frame_poll = time.monotonic()

    def state(self):
        self.capture()
        uart = self.uart.read_text(errors="replace") if self.uart.exists() else ""
        guest_booted = "entry 0x" in uart or "Entering activity:" in uart
        panic = "Guru Meditation" in uart or "abort() was called" in uart
        return {"ready": self.frames > 0 and guest_booted and not panic,
                "guest_booted": guest_booted, "boot_failed": panic,
                "frames": self.frames, "sha256": self.frame_sha,
                "nonwhite": self.nonwhite, "dimensions": [W, H],
                "rom": {"name": self.rom.name, "sha256": file_sha(self.rom),
                        "provenance_note": "eink-qemu 0.9.2 bundled ROM"},
                "firmware": {"name": self.fw_name, "sha256": self.fw_sha,
                             "provenance_note": "original unmodified flash"},
                "media": {"persistent": True, "initial_guest_flash_sha256":
                          self.media_initial["flash"], "initial_guest_sd_sha256":
                          self.media_initial["sd"]},
                "engine": {"name": "serialx/eink-qemu", "version": "0.9.2",
                           "qemu": "11.1.2", "wifi_nat": self.args.wifi},
                "power": {"available": self.args.enable_power,
                          "pressed": self.power_pressed},
                "boot_wait_seconds": round(time.monotonic() - self.boot_time, 2)}

    def set_power(self, pressed):
        """Opt-in virtual board Power key with a nonrenewable 1.2s watchdog."""
        if not self.args.enable_power:
            raise RuntimeError("virtual Power is disabled on this QEMU11 bridge")
        if type(pressed) is not bool:
            raise ValueError("pressed must be a JSON boolean")
        with self.lock:
            if pressed:
                if not self.power_pressed:
                    self.qmp("qom-set", {"path": "/machine/x4pro-keys",
                                         "property": "power", "value": True})
                    self.power_pressed = True
                    self.power_generation += 1
                    generation = self.power_generation
                    self.power_timer = threading.Timer(
                        1.2, self._auto_release_power, args=(generation,))
                    self.power_timer.daemon = True
                    self.power_timer.start()
            else:
                if self.power_timer is not None:
                    self.power_timer.cancel()
                    self.power_timer = None
                if self.power_pressed:
                    try:
                        self.qmp("qom-set", {"path": "/machine/x4pro-keys",
                                             "property": "power", "value": False})
                    except Exception:
                        self.stop()  # uncertain held key: fail closed, no false release
                        raise
                    self.power_pressed = False
                    self.power_generation += 1
            return {"action": "virtual-power", "pressed": self.power_pressed}

    def _auto_release_power(self, generation):
        with self.lock:
            if generation != self.power_generation or not self.power_pressed:
                return
            try:
                self.set_power(False)
            except Exception:
                self.stop()  # release could not be confirmed

    def tap(self, x, y, duration):
        if type(x) is not int or type(y) is not int or not (0 <= x < W and 0 <= y < H):
            raise ValueError("touch coordinates must be within 480x800")
        if type(duration) is not int or not (30 <= duration <= 1000):
            raise ValueError("invalid touch duration")
        xv = round(32767 * x / (W - 1))
        yv = round(32767 * y / (H - 1))
        self.qmp("input-send-event", {"events": [
            {"type": "abs", "data": {"axis": "x", "value": xv}},
            {"type": "abs", "data": {"axis": "y", "value": yv}},
            {"type": "btn", "data": {"button": "left", "down": True}}]})
        try:
            time.sleep(duration / 1000)
        finally:
            self.qmp("input-send-event", {"events": [
                {"type": "btn", "data": {"button": "left", "down": False}}]})
        return {"accepted": True, "x": x, "y": y, "duration_ms": duration}

    def firmware_list(self):
        choices = []
        for path in sorted(self.args.firmware_dir.glob("*.bin")):
            if path.stat().st_size != 16 * 1024 * 1024:
                continue
            choices.append({"name": path.name, "size": path.stat().st_size,
                            "sha256": file_sha(path), "kind": "flash",
                            "active": path.name == self.fw_name})
        return {"firmwares": choices, "truncated": False}

    def firmware_switch(self, name):
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError("invalid firmware name")
        target = self.args.firmware_dir / name
        if not target.is_file() or target.stat().st_size != self.flash.stat().st_size:
            raise ValueError("firmware absent or incompatible flash image size")
        with self.lock:
            self.stop()
            staged = self.flash.with_suffix(".bin.new")
            shutil.copyfile(target, staged)
            os.replace(staged, self.flash)
            self.fw_name, self.fw_sha = target.name, file_sha(target)
            record = self.sidecar.with_suffix(".json.new")
            record.write_text(json.dumps({"name": self.fw_name}) + "\n")
            os.replace(record, self.sidecar)
            self.start()
        return {"name": self.fw_name, "sha256": self.fw_sha,
                "provenance_note": "original unmodified flash"}

    def reboot(self):
        if self.power_pressed:
            self.set_power(False)
        self.qmp("system_reset")
        with self.lock:
            self.png = b""
            self.frame_sha = ""
            self.frames = 0
            self.last_frame_poll = 0
        return {"action": "reboot", "accepted": True}


def serve(args):
    engine = Qemu11(args)
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, data, content_type="application/json"):
            raw = json.dumps(data).encode() if not isinstance(data, bytes) else data
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def post_json(self):
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ValueError("requires application/json")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024:
                raise ValueError("invalid JSON body size")
            value = json.loads(self.rfile.read(size))
            if not isinstance(value, dict):
                raise ValueError("expected JSON object")
            return value

        def do_GET(self):
            path = urlparse(self.path).path
            try:
                if path in ("/", "/index.html"):
                    self.reply(200, PAGE.encode(), "text/html; charset=utf-8")
                elif path == "/api/state":
                    self.reply(200, engine.state())
                elif path == "/screen.png":
                    engine.capture()
                    self.reply(200, engine.png, "image/png")
                elif path == "/api/uart":
                    log = engine.uart.read_text(errors="replace") if engine.uart.exists() else ""
                    limit = min(65536, max(128, int(parse_qs(urlparse(self.path).query)
                                                       .get("tail", ["4096"])[0])))
                    self.reply(200, {"available": bool(log), "bytes": len(log.encode()),
                                     "limit": limit, "tail": log[-limit:]})
                elif path == "/api/firmware":
                    self.reply(200, engine.firmware_list())
                elif path == "/api/roms":
                    self.reply(200, {"roms": [], "truncated": False,
                                     "note": "QEMU11 uses a verified bundled ROM"})
                elif path == "/api/telemetry":
                    self.reply(200, {"frame-sha256": {
                        "available": bool(engine.frame_sha), "value": engine.frame_sha},
                        "ybx-epd-frames": {"available": False},
                        "ybx-epd-busy-rise-edges": {"available": False},
                        "ybx-epd-busy-fall-edges": {"available": False},
                        "ybx-epd-busy-high-ms": {"available": False}})
                else:
                    self.reply(404, {"error": "unknown endpoint"})
            except (ValueError, RuntimeError, OSError) as err:
                self.reply(503, {"error": str(err)})

        def do_POST(self):
            try:
                if self.path == "/api/tap":
                    v = self.post_json()
                    self.reply(200, engine.tap(v.get("x"), v.get("y"),
                                               v.get("duration_ms", 150)))
                elif self.path == "/api/reboot":
                    self.reply(200, engine.reboot())
                elif self.path == "/api/firmware":
                    v = self.post_json()
                    self.reply(200, engine.firmware_switch(v.get("name")))
                elif self.path == "/api/power":
                    v = self.post_json()
                    self.reply(200, engine.set_power(v.get("pressed")))
                elif self.path == "/api/rom":
                    self.reply(409, {"error": "QEMU11 uses the bundled ROM"})
                else:
                    self.reply(404, {"error": "unknown endpoint"})
            except ValueError as err:
                self.reply(400, {"error": str(err)})
            except (RuntimeError, OSError) as err:
                self.reply(503, {"error": str(err)})

        def log_message(self, fmt, *values):
            print(f"[eink11-http] {self.address_string()} " + fmt % values, flush=True)

    try:
        ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    finally:
        engine.stop()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--qemu", type=Path, required=True)
    p.add_argument("--rom-dir", type=Path, required=True)
    p.add_argument("--flash", type=Path, required=True)
    p.add_argument("--sd", type=Path, required=True)
    p.add_argument("--firmware-dir", type=Path, required=True)
    p.add_argument("--work-dir", type=Path, required=True)
    p.add_argument("--wifi", action="store_true", help="QEMU usermode NAT via PICSimLabWifi")
    p.add_argument("--enable-power", action="store_true",
                   help="opt-in 1.2s bounded virtual X4 Pro Power key")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--container-bind", action="store_true",
                   help="bind container interfaces only when Docker publishes to 127.0.0.1")
    p.add_argument("--port", type=int, default=8806)
    args = p.parse_args()
    if args.port == 8784 or (args.host != "127.0.0.1" and not
                             (args.container_bind and args.host == "0.0.0.0")):
        p.error("QEMU11 migration must be private and must not bind production 8784")
    serve(args)
