"""Portable YBX QEMU11 browser adapter contracts; no Docker in CI."""
import os
from pathlib import Path
import subprocess
import struct
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import zlib

from serve_einkqemu11_browser import (
    W, H, PAGE, KNOWN_BINARY_SHA, Qemu11, choose_media, decode_png_rgb
)

LAUNCHER = Path(__file__).with_name("start_einkqemu11_browser.sh")


def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(
        ">I", zlib.crc32(tag + data) & 0xffffffff)


def test_png(rgb=(239, 239, 239), filt=0):
    stride = W * 3
    actual = bytes(rgb) * W
    filtered = bytearray()
    for y in range(H):
        if filt == 0:
            data = actual
        elif filt == 1:
            data = bytes((v - (actual[i - 3] if i >= 3 else 0)) & 255
                         for i, v in enumerate(actual))
        elif filt == 2:
            data = actual if y == 0 else bytes(stride)
        else:
            raise AssertionError("invalid fixture filter")
        filtered.append(filt if y > 0 else (filt if filt != 2 else 0))
        filtered.extend(data)
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
    return (bytes.fromhex("89504e470d0a1a0a")
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(filtered))
            + chunk(b"IEND", b""))


class X4ProQemu11AdapterTests(unittest.TestCase):
    def test_png_rgb_matches_real_panel_dimensions_and_values(self):
        for color in ((239, 239, 239), (16, 16, 16), (255, 255, 255)):
            for kind in (0, 1, 2):
                with self.subTest(color=color, filter=kind):
                    self.assertEqual(
                        decode_png_rgb(test_png(color, kind)),
                        bytes(color) * W * H)

    def test_malformed_frame_refused(self):
        with self.assertRaises(ValueError):
            decode_png_rgb(b"\x89PNG" + bytes(100))
        with self.assertRaises(ValueError):
            decode_png_rgb(test_png()[:-13])

    def test_saved_flash_and_sd_survive_reentry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_flash = root / "source-flash"
            source_sd = root / "source-sd"
            source_flash.write_bytes(b"f" * 64)
            source_sd.write_bytes(b"s" * 512)
            path = root / "save"
            a, b = choose_media(source_flash, source_sd, path)
            self.assertEqual((a.read_bytes(), b.read_bytes()),
                             (source_flash.read_bytes(), source_sd.read_bytes()))
            a.write_bytes(b"x" * 64)
            b.write_bytes(b"y" * 512)
            aa, bb = choose_media(source_flash, source_sd, path)
            self.assertEqual((aa.read_bytes(), bb.read_bytes()),
                             (b"x" * 64, b"y" * 512))
            self.assertEqual(source_sd.read_bytes(), b"s" * 512)

    def test_partial_and_wrong_size_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f, sd = root / "source-flash", root / "source-sd"
            f.write_bytes(bytes(64))
            sd.write_bytes(bytes(512))
            save = root / "save"
            save.mkdir()
            (save / "flash.bin").write_bytes(bytes(64))
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                choose_media(f, sd, save)
            self.assertFalse((save / "sd.img").exists())
            (save / "sd.img").write_bytes(bytes(511))
            with self.assertRaisesRegex(RuntimeError, "wrong size"):
                choose_media(f, sd, save)

    def test_symlink_disk_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f, sd = root / "source-flash", root / "source-sd"
            f.write_bytes(bytes(64))
            sd.write_bytes(bytes(512))
            save = root / "save"
            save.mkdir()
            (save / "flash.bin").symlink_to(f)
            with self.assertRaisesRegex(RuntimeError, "symlinks"):
                choose_media(f, sd, save)

    def test_stable_port_rejected_before_docker(self):
        env = {**os.environ, "YBX_EINK11_PORT": "8784"}
        r = subprocess.run(["bash", str(LAUNCHER)], env=env,
                           capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 2)
        self.assertIn("8784", r.stderr)

    def test_ui_replaces_obsolete_ssd1677_and_no_wifi_disclaimers(self):
        self.assertIn("UC8279", PAGE)
        self.assertIn("Native Wi-Fi MAC available", PAGE)
        self.assertNotIn("Wi-Fi radio is not emulated", PAGE)
        self.assertNotIn("real guest-written SSD1677", PAGE)

    def test_pinned_binary_and_no_guest_network_by_default(self):
        self.assertEqual(len(KNOWN_BINARY_SHA), 64)
        code = LAUNCHER.read_text()
        self.assertIn('127.0.0.1:$PORT:$PORT', code)
        self.assertIn('YBX_EINK11_WIFI:-0', code)
        self.assertIn(':ro', code)
        self.assertIn('/persist:rw', code)
        self.assertIn('KNOWN_BINARY_SHA', Path(__file__).with_name(
            "serve_einkqemu11_browser.py").read_text())


class PowerWatchdogSafetyTests(unittest.TestCase):
    def backend(self, enabled=True):
        obj = Qemu11.__new__(Qemu11)
        obj.args = SimpleNamespace(enable_power=enabled)
        obj.lock = threading.RLock()
        obj.power_pressed = False
        obj.power_generation = 0
        obj.power_timer = None
        obj.proc = None
        obj.qmp = Mock(return_value={})
        return obj

    def test_power_opt_in_and_strict_bool(self):
        disabled = self.backend(False)
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            disabled.set_power(True)
        enabled = self.backend()
        with self.assertRaisesRegex(ValueError, "JSON boolean"):
            enabled.set_power(1)
        self.assertEqual(enabled.qmp.call_count, 0)

    def test_bounded_power_and_stale_watchdog(self):
        engine = self.backend()
        self.assertEqual(engine.set_power(True)["pressed"], True)
        first = engine.power_generation
        timer = engine.power_timer
        engine.set_power(True)  # repeat may NOT renew the hold deadline
        self.assertIs(engine.power_timer, timer)
        self.assertEqual(engine.power_generation, first)
        self.assertEqual(engine.qmp.call_count, 1)
        engine.set_power(False)
        self.assertFalse(engine.power_pressed)
        self.assertEqual(engine.set_power(True)["pressed"], True)
        self.assertGreater(engine.power_generation, first)
        engine._auto_release_power(first)  # stale callbacks cannot release new press
        self.assertTrue(engine.power_pressed)
        engine._auto_release_power(engine.power_generation)
        self.assertFalse(engine.power_pressed)
        self.assertEqual(
            [call.args[1]["value"] for call in engine.qmp.call_args_list],
            [True, False, True, False])

    def test_power_uses_native_board_qom_not_system_reset(self):
        engine = self.backend()
        engine.set_power(True)
        engine.set_power(False)
        for call in engine.qmp.call_args_list:
            self.assertEqual(call.args[0], "qom-set")
            self.assertEqual(call.args[1]["path"], "/machine/x4pro-keys")
            self.assertEqual(call.args[1]["property"], "power")


if __name__ == "__main__":
    unittest.main()
