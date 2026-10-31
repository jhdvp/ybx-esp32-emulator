"""Host-only guardrails for the X4 Pro QEMU browser click bridge."""
import email.utils
import hashlib
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from serve_x4pro_browser import (KNOWN_FACTORY_IMAGES, PAGE, BadRequestError,
                                 FactoryX4Pro, GuestBusyError,
                                 GuestMissingError, RomNotFoundError,
                                 TapQueueFullError, check_touch, etag_matches,
                                 guest_ready_for_touch, make_handler,
                                 not_modified_since, sha256_file, W, H)
from http.server import ThreadingHTTPServer

FACTORY_ROM_SHA = ("5b950be93a30610322a3b46049e4095cdda04f33f09fa13a9a"
                   "9a6167b5994bd9")
FACTORY_FLASH_SHA = ("206ed9661bd71477d1a54397c5a2cfe490179a3f59ab094f1e"
                     "7421745c609523")


def browser_guest(**overrides):
    """FactoryX4Pro instance without a real QEMU process or socket."""
    guest = FactoryX4Pro.__new__(FactoryX4Pro)
    guest.lock = threading.RLock()
    guest.tap_lock = threading.Lock()
    guest.tap_slots = threading.BoundedSemaphore(FactoryX4Pro.tap_queue_depth)
    guest.process = Mock(name="process", poll=Mock(return_value=None))
    guest.client = Mock()
    guest.frame = 42
    guest.sha = "old-sha"
    guest.png = b"old-frame"
    guest.blank_png = b"blank-frame"
    guest.png_etag = '"f42-old-sha"'
    guest.png_mtime = 1_700_000_000.0
    guest.qmp = Mock(return_value={"return": {}})
    guest.firmware_name = "factory.bin"
    guest.firmware_sha = FACTORY_FLASH_SHA
    guest.firmware_path = Path(tempfile.mkdtemp(prefix="ybx-x4pro-fwpath-")) \
        / "factory.bin"
    guest.firmware_path.write_bytes(b"\xe9" + b"\xff" * 0x20000)
    guest.boot_started_at = time.monotonic()
    guest._launch = Mock()
    guest._stop_process = Mock()
    for key, value in overrides.items():
        setattr(guest, key, value)
    return guest


def rom_guest(**overrides):
    """browser_guest plus real work/roms directories on disk."""
    guest = browser_guest()
    root = Path(tempfile.mkdtemp(prefix="ybx-x4pro-rom-"))
    guest.guest_tmpdir = root
    guest.workdir = root / "work"
    (guest.workdir / "rom").mkdir(parents=True)
    guest.rom_dir = root / "roms"
    guest.rom_dir.mkdir()
    guest.rom_name = "factory.bin"
    guest.rom_sha = FACTORY_ROM_SHA
    for key, value in overrides.items():
        setattr(guest, key, value)
    return guest


def make_rom_file(guest, name, payload):
    (guest.rom_dir / name).write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


class BrowserTouchTests(unittest.TestCase):
    def test_readiness_does_not_depend_on_nonwhite_pixel_count(self):
        # Frame 1 is a partial/splash frame; subsequent real screens may
        # contain very little ink, including the reported frame 9.
        self.assertFalse(guest_ready_for_touch(0))
        self.assertFalse(guest_ready_for_touch(1))
        self.assertTrue(guest_ready_for_touch(2))
        self.assertTrue(guest_ready_for_touch(9))

    def test_correct_more_and_settings_coordinates(self):
        self.assertEqual(check_touch({"x":350,"y":760}),(350,760,150))
        self.assertEqual(check_touch({"x":240,"y":453,"duration_ms":180}),
                         (240,453,180))

    def test_all_panel_corners(self):
        self.assertEqual(check_touch({"x":0,"y":0}),(0,0,150))
        self.assertEqual(check_touch({"x":W-1,"y":H-1}),(479,799,150))

    def test_out_of_bounds_rejected(self):
        for x,y in ((-1,10),(480,20),(400,-1),(400,800)):
            with self.subTest(x=x,y=y):
                with self.assertRaises(ValueError):
                    check_touch({"x":x,"y":y})

    def test_invalid_or_oversized_payload_rejected(self):
        for data in (None,{"x":True,"y":20},{"x":"1","y":0},
                     {"x":12.2,"y":3},{"x":1,"y":4,"duration_ms":1000},
                     {"x":0,"y":0,"duration_ms":0},{}):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    check_touch(data)

    def test_reboot_clears_gt911_session_before_real_qmp_reset(self):
        guest = browser_guest()
        guest.reboot()
        calls = guest.qmp.call_args_list
        self.assertEqual([c.args[0] for c in calls],
                         ["qom-set", "qom-set", "qom-set", "system_reset"])
        self.assertEqual([c.args[1]["property"] for c in calls[:3]],
                         ["ybx-touch-state", "ybx-touch-x", "ybx-touch-y"])
        self.assertTrue(all(c.args[1]["value"] == 0 for c in calls[:3]))
        guest._launch.assert_not_called()
        self.assertEqual(guest.frame, -1)
        self.assertEqual(guest.sha, "")
        self.assertEqual(guest.png, guest.blank_png)
        self.assertEqual(guest.png_etag, '"blank"')
        self.assertFalse(guest.tap_lock.locked())
        self.assertTrue(guest.tap_slots.acquire(blocking=False))

    def test_reboot_qmp_error_preserves_guest_frame(self):
        guest = browser_guest(qmp=Mock(side_effect=[{"return": {}}] * 3
                                    + [{"error": {"desc": "reset unavailable"}}]))
        with self.assertRaises(RuntimeError):
            guest.reboot()
        self.assertEqual(guest.frame, 42)
        self.assertEqual(guest.png, b"old-frame")
        guest._launch.assert_not_called()
        self.assertFalse(guest.tap_lock.locked())

    def test_reboot_touch_clear_failure_skips_reset(self):
        guest = browser_guest(qmp=Mock(return_value={"error": {"desc": "invalid"}}))
        with self.assertRaises(RuntimeError):
            guest.reboot()
        self.assertEqual(guest.qmp.call_count, 1)
        self.assertEqual(guest.frame, 42)
        self.assertEqual(guest.png, b"old-frame")
        self.assertFalse(guest.tap_lock.locked())

    def test_reboot_of_terminated_guest_relaunches_process(self):
        dead = Mock(name="process", poll=Mock(return_value=1))
        guest = browser_guest(process=dead)
        guest.reboot()
        guest.qmp.assert_not_called()
        guest._launch.assert_called_once_with()
        self.assertEqual(guest.frame, -1)
        self.assertEqual(guest.png, guest.blank_png)
        self.assertFalse(guest.tap_lock.locked())

    def test_reboot_relaunches_guest_when_qmp_socket_is_broken(self):
        for fault in (BrokenPipeError("socket closed"),
                      RuntimeError("QMP closed")):
            with self.subTest(fault=fault):
                guest = browser_guest(qmp=Mock(side_effect=fault))
                guest.reboot()
                guest._launch.assert_called_once_with()
                self.assertEqual(guest.frame, -1)
                self.assertFalse(guest.tap_lock.locked())

    def test_failed_relaunch_keeps_lock_for_another_recovery_attempt(self):
        guest = browser_guest(qmp=Mock(side_effect=BrokenPipeError("x")),
                              _launch=Mock(side_effect=ConnectionRefusedError("no socket")))
        with self.assertRaises(OSError):
            guest.reboot()
        self.assertFalse(guest.tap_lock.locked())
        self.assertTrue(guest.tap_lock.acquire(blocking=False))
        guest.tap_lock.release()

    def test_state_of_terminated_guest_points_at_recovery(self):
        guest = browser_guest(process=Mock(poll=Mock(return_value=1)))
        with self.assertRaises(RuntimeError) as caught:
            guest.state()
        self.assertIn("Restart virtual reader", str(caught.exception))

    def test_simultaneous_taps_serialize_instead_of_being_rejected(self):
        events = []
        def qmp(cmd, args=None):
            if args["property"] == "ybx-touch-state":
                events.append(args["value"])
            return {"return": {}}
        guest = browser_guest(qmp=qmp)
        guest.state = Mock(return_value={"ready": True})
        barrier = threading.Barrier(2)
        def tap_twice():
            barrier.wait()
            guest.tap(10, 20, 60)
        threads = [threading.Thread(target=tap_twice) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)
        self.assertEqual(events, [1, 0, 1, 0])

    def test_tap_times_out_when_guest_stays_busy(self):
        guest = browser_guest()
        guest.tap_wait_seconds = 0.05
        guest.tap_lock.acquire()
        try:
            with self.assertRaises(GuestBusyError) as caught:
                guest.tap(1, 2, 60)
        finally:
            self.assertTrue(guest.tap_lock.locked())
            guest.tap_lock.release()
        self.assertIn("busy", str(caught.exception))
        self.assertEqual(caught.exception.kind, "guest-busy")
        # The rejected waiter must hand its queue slot back.
        for _ in range(guest.tap_queue_depth):
            self.assertTrue(guest.tap_slots.acquire(blocking=False))

    def test_tap_releases_lock_when_qmp_dies_mid_touch(self):
        guest = browser_guest(qmp=Mock(side_effect=[{"return": 0},
                                                    {"return": 0},
                                                    BrokenPipeError("QMP closed")]))
        guest.state = Mock(return_value={"ready": True})
        with self.assertRaises(OSError):
            guest.tap(1, 2, 60)
        self.assertFalse(guest.tap_lock.locked())

    def test_reboot_touch_clear_failure_skips_reset(self):
        guest = browser_guest(qmp=Mock(return_value={"error": {"desc": "no property"}}))
        with self.assertRaises(RuntimeError):
            guest.reboot()
        self.assertEqual(guest.qmp.call_count, 1)
        self.assertEqual(guest.frame, 42)
        self.assertFalse(guest.tap_lock.locked())

    def test_browser_explains_unsupported_wifi_and_offers_reboot(self):
        self.assertIn("Wi-Fi radio is not emulated", PAGE)
        self.assertIn('/api/reboot', PAGE)
        self.assertIn('Restart virtual reader', PAGE)
        self.assertIn("confirm(", PAGE)

    def test_html_refuses_fake_navigation(self):
        self.assertIn("/api/tap",PAGE)
        self.assertIn("/screen.png",PAGE)
        self.assertIn("real guest-written SSD1677",PAGE)
        self.assertNotIn("More menu opened successfully",PAGE)

    def test_html_offers_process_recovery_and_ios_guard(self):
        self.assertIn("starts a fresh guest", PAGE)
        self.assertIn("if(!rect.width||!rect.height)return;", PAGE)

    def test_page_polls_telemetry_and_drops_png_cache_buster(self):
        self.assertIn("/api/telemetry", PAGE)
        self.assertIn("BUSY telemetry", PAGE)
        # ETag/304 replace the old per-request Date.now() cache-buster.
        self.assertNotIn("Date.now()", PAGE)


def telemetry_qmp(values):
    """QMP stub answering the four read-only qom-get counter probes."""
    calls = []
    def qmp(cmd, args=None):
        calls.append((cmd, args))
        if cmd == "qom-get":
            if args["property"] in values:
                return {"return": values[args["property"]]}
            return {"error": {"class": "GenericError",
                              "desc": f"Property 'soc.{args['property']}' not found"}}
        return {"return": {}}
    return qmp, calls


class TelemetryTests(unittest.TestCase):
    COUNTERS = ("ybx-epd-busy-rise-edges", "ybx-epd-busy-fall-edges",
                "ybx-epd-busy-high-ms", "ybx-epd-frames")

    def test_telemetry_reports_counters_and_frame_sha(self):
        qmp, calls = telemetry_qmp({
            "ybx-epd-busy-rise-edges": 17, "ybx-epd-busy-fall-edges": 12,
            "ybx-epd-busy-high-ms": 350, "ybx-epd-frames": 9})
        guest = browser_guest(qmp=qmp, sha="a"*64)
        counters = guest.telemetry()
        self.assertEqual(sorted(counters), sorted(self.COUNTERS + ("frame-sha256",)))
        for name, value in (("ybx-epd-busy-rise-edges", 17),
                            ("ybx-epd-busy-fall-edges", 12),
                            ("ybx-epd-busy-high-ms", 350),
                            ("ybx-epd-frames", 9)):
            with self.subTest(name=name):
                self.assertEqual(counters[name],
                                 {"available": True, "value": value,
                                  "source": f"qom:/machine/soc/{name}"})
        self.assertEqual(counters["frame-sha256"],
                         {"available": True, "value": "a"*64,
                          "source": "server:sha256(ssd1677-visible-ram)"})

    def test_telemetry_is_read_only(self):
        qmp, calls = telemetry_qmp({"ybx-epd-frames": 1})
        browser_guest(qmp=qmp).telemetry()
        self.assertTrue(all(cmd == "qom-get" for cmd, _ in calls), calls)
        self.assertEqual({args["property"] for _, args in calls} &
                         {"ybx-touch-state", "ybx-touch-x", "ybx-touch-y"}, set())

    def test_missing_counters_are_unavailable_never_a_zero(self):
        qmp, _ = telemetry_qmp({"ybx-epd-frames": 9})
        counters = browser_guest(qmp=qmp).telemetry()
        for name in ("ybx-epd-busy-rise-edges", "ybx-epd-busy-fall-edges",
                     "ybx-epd-busy-high-ms"):
            with self.subTest(name=name):
                self.assertFalse(counters[name]["available"])
                self.assertIsNone(counters[name]["value"])
                self.assertIn("not found", counters[name]["error"])
        self.assertEqual(counters["ybx-epd-frames"],
                         {"available": True, "value": 9,
                          "source": "qom:/machine/soc/ybx-epd-frames"})

    def test_frame_sha_unreported_before_first_hashed_frame(self):
        qmp, _ = telemetry_qmp({"ybx-epd-frames": 0})
        counters = browser_guest(qmp=qmp, sha="").telemetry()
        self.assertEqual(counters["frame-sha256"],
                         {"available": False, "value": None,
                          "error": "no rendered frame has been hashed yet"})

    def test_telemetry_of_terminated_guest_is_guest_missing(self):
        guest = browser_guest(process=Mock(poll=Mock(return_value=1)))
        with self.assertRaises(GuestMissingError):
            guest.telemetry()

    def test_telemetry_transport_failure_is_guest_missing_not_blank_counters(self):
        guest = browser_guest(qmp=Mock(side_effect=BrokenPipeError("QMP closed")))
        with self.assertRaises(GuestMissingError) as caught:
            guest.telemetry()
        self.assertIn("QMP closed", str(caught.exception))


class TapQueueTests(unittest.TestCase):
    def test_queue_rejects_the_fifth_waiting_tap(self):
        guest = browser_guest()
        guest.state = Mock(return_value={"ready": True})
        guest.qmp = Mock(return_value={"return": 0})
        held = [guest.tap_slots.acquire(blocking=False)
                for _ in range(guest.tap_queue_depth)]
        self.assertTrue(all(held))
        with self.assertRaises(TapQueueFullError) as caught:
            guest.tap(1, 2, 60)
        self.assertEqual(caught.exception.kind, "guest-busy")
        self.assertEqual(caught.exception.http_status, 429)
        self.assertFalse(guest.tap_lock.locked())
        guest.qmp.assert_not_called()

    def test_completed_tap_returns_its_queue_slot(self):
        guest = browser_guest(qmp=Mock(return_value={"return": 0}))
        guest.state = Mock(return_value={"ready": True})
        guest.tap(1, 2, 60)
        for _ in range(guest.tap_queue_depth):
            self.assertTrue(guest.tap_slots.acquire(blocking=False))

    def test_failed_qmp_release_returns_every_slot(self):
        guest = browser_guest(qmp=Mock(side_effect=BrokenPipeError("QMP closed")))
        guest.state = Mock(return_value={"ready": True})
        with self.assertRaises(OSError):
            guest.tap(1, 2, 60)
        for _ in range(guest.tap_queue_depth):
            self.assertTrue(guest.tap_slots.acquire(blocking=False))

    def test_three_concurrent_taps_serialize_without_interleaving(self):
        events = []
        def qmp(cmd, args=None):
            if args["property"] == "ybx-touch-state":
                events.append(args["value"])
            return {"return": {}}
        guest = browser_guest(qmp=qmp)
        guest.state = Mock(return_value={"ready": True})
        barrier = threading.Barrier(3)
        def tap():
            barrier.wait()
            guest.tap(10, 20, 60)
        threads = [threading.Thread(target=tap) for _ in range(3)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)
        self.assertEqual([e for e in events], [1, 0] * 3)
        for _ in range(guest.tap_queue_depth):
            self.assertTrue(guest.tap_slots.acquire(blocking=False))


class ConditionalRequestTests(unittest.TestCase):
    def test_etag_matches_strong_weak_lists_and_star(self):
        self.assertTrue(etag_matches('"abc"', '"abc"'))
        self.assertTrue(etag_matches('W/"abc"', '"abc"'))
        self.assertTrue(etag_matches('"/other", "abc"', '"abc"'))
        self.assertTrue(etag_matches("*", '"abc"'))
        self.assertFalse(etag_matches('"abd"', '"abc"'))
        self.assertFalse(etag_matches(None, '"abc"'))

    def test_if_modified_since_is_second_grained(self):
        mtime = 1_700_000_000.9
        stamp = email.utils.formatdate(1_700_000_000, usegmt=True)
        self.assertTrue(not_modified_since(stamp, mtime))
        self.assertFalse(not_modified_since(
            email.utils.formatdate(1_699_999_999, usegmt=True), mtime))
        self.assertFalse(not_modified_since("garbage", mtime))


class FakeGuest:
    PNG_ETAG = '"f3-0123456789abcdef"'
    PNG_MTIME = 1_700_000_000.0
    ROM_ITEMS = (
        {"name": "a-custom.bin", "size": 10, "sha256": "c" * 64,
         "provenance": "custom",
         "provenance_note": "Custom ROM of unknown provenance "
                            "(SHA-256 matches no known factory image)"},
        {"name": "factory.bin", "size": 20, "sha256": FACTORY_ROM_SHA,
         "provenance": "factory-rom",
         "provenance_note": "Factory ROM image (SHA-256 matches the known "
                            "factory -rom blob)"},
    )

    def __init__(self):
        self.taps = []
        self.reboots = 0
        self.png = b"\x89PNG fake"
        self.png_etag = self.PNG_ETAG
        self.png_mtime = self.PNG_MTIME
        self.state_error = None
        self.tap_error = None
        self.reboot_error = None
        self.telemetry_error = None
        self.tap_delay = 0.0
        self.rom_name = "factory.bin"
        self.rom_switches = []
        self.rom_error = None
        self.rom_list_error = None
        self.frames = 3
        self.boot_wait_seconds = 3
        self.boot_failed = False
        self.firmware_items = ()

    def firmware_info(self):
        return {"name": "factory.bin", "sha256": FACTORY_FLASH_SHA,
                "provenance": "factory-flash",
                "provenance_note": "Factory flash image",
                "known_factory_image": True}

    def firmwares(self):
        return {"firmwares": list(self.firmware_items),
                "active": "factory.bin", "truncated": False,
                "limit": 64, "max_hash_bytes": 64 * 1024 * 1024}

    def switch_firmware(self, name):
        self.firmware_switches.append(name)
        return {"name": name, "sha256": "b" * 64, "kind": "flash",
                "provenance": "custom",
                "provenance_note": "Custom ROM of unknown provenance"}

    def rom_info(self):
        item = next(i for i in self.ROM_ITEMS if i["name"] == self.rom_name)
        return {"name": item["name"], "sha256": item["sha256"],
                "provenance": item["provenance"],
                "provenance_note": item["provenance_note"],
                "known_factory_image":
                    item["provenance"].startswith("factory-")}

    def state(self):
        if self.state_error:
            raise self.state_error
        return {"frames": self.frames, "monotonic_ms": 4321,
                "nonwhite": 1234,
                "ready": guest_ready_for_touch(self.frames),
                "guest_booted": bool(self.frames > 0),
                "boot_wait_seconds": self.boot_wait_seconds,
                "boot_failed": self.boot_failed,
                "sha256": "0" * 64, "factory_guest": True,
                "firmware": self.firmware_info(),
                "rom": self.rom_info(),
                "physical_device": False}

    def roms(self):
        if self.rom_list_error:
            raise self.rom_list_error
        return {"roms": [dict(item, active=item["name"] == self.rom_name)
                         for item in self.ROM_ITEMS],
                "active": self.rom_name, "truncated": False,
                "limit": 64, "max_hash_bytes": 64 * 1024 * 1024}

    def switch_rom(self, name):
        if self.rom_error:
            raise self.rom_error
        item = next((i for i in self.ROM_ITEMS if i["name"] == name), None)
        if item is None:
            raise RomNotFoundError(f"Unknown ROM: {name}")
        self.rom_switches.append(name)
        self.rom_name = name
        self.png = b"blank-frame"
        self.png_etag = '"blank"'
        self.png_mtime = time.time()
        return {"name": name, "sha256": item["sha256"],
                "provenance": item["provenance"],
                "provenance_note": item["provenance_note"]}

    def telemetry(self):
        if self.telemetry_error:
            raise self.telemetry_error
        counters = {}
        for name, value in (("ybx-epd-busy-rise-edges", 17),
                            ("ybx-epd-busy-fall-edges", 12),
                            ("ybx-epd-busy-high-ms", 350),
                            ("ybx-epd-frames", 3)):
            counters[name] = {"available": True, "value": value,
                              "source": f"qom:/machine/soc/{name}"}
        counters["frame-sha256"] = {"available": False, "value": None,
                                    "error": "no rendered frame has been hashed yet"}
        return counters

    def tap(self, x, y, ms):
        if self.tap_error:
            raise self.tap_error
        if self.tap_delay:
            time.sleep(self.tap_delay)
        self.taps.append((x, y, ms))

    def reboot(self):
        if self.reboot_error:
            raise self.reboot_error
        self.reboots += 1


class ServerHarnessMixin:
    """Shared in-process HTTP plumbing; no Docker, no guest, no network."""

    def setUp(self):
        self.guest = FakeGuest()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          make_handler(self.guest))
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02},
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None, raw=False):
        conn = http.client.HTTPConnection("127.0.0.1",
                                          self.server.server_address[1],
                                          timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            payload = response.read()
            return response.status, payload
        finally:
            conn.close()

    def json_request(self, *args, **kwargs):
        status, payload = self.request(*args, **kwargs)
        return status, json.loads(payload)

    def full_request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1",
                                          self.server.server_address[1],
                                          timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            payload = response.read()
            return response.status, dict(response.getheaders()), payload
        finally:
            conn.close()


class HttpContractTests(ServerHarnessMixin, unittest.TestCase):
    """Drive make_handler through a real socket with an in-process guest."""

    def test_state_and_screen_png_serve_the_guest(self):
        status, body = self.json_request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(body["ready"])
        self.assertFalse(body["physical_device"])
        status, payload = self.request("GET", "/screen.png")
        self.assertEqual((status, payload), (200, self.guest.png))

    def test_page_served_as_html(self):
        status, payload = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"YBX X4 Pro", payload)

    def test_non_numeric_content_length_gets_400(self):
        status, body = self.json_request(
            "POST", "/api/tap", body=None,
            headers={"Content-Type": "application/json",
                     "Content-Length": "abc"})
        self.assertEqual(status, 400)
        self.assertIn("Content-Length", body["error"])
        self.assertEqual(self.guest.taps, [])
        alive, _ = self.request("GET", "/")
        self.assertEqual(alive, 200)

    def test_missing_content_length_gets_400(self):
        conn = http.client.HTTPConnection("127.0.0.1",
                                         self.server.server_address[1],
                                         timeout=5)
        conn.putrequest("POST", "/api/tap")
        conn.putheader("Content-Type", "application/json")
        conn.endheaders()
        status = conn.getresponse().status
        conn.close()
        self.assertEqual(status, 400)

    def test_oversized_payload_gets_400(self):
        body = b'{"x":1,"y":1,"pad":"' + b"z" * 200 + b'"}'
        status, payload = self.json_request(
            "POST", "/api/tap", body=body,
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertIn("size", payload["error"])

    def test_wrong_content_type_and_bad_json_get_400(self):
        status, _ = self.json_request(
            "POST", "/api/tap", body=b'{"x":1,"y":1}',
            headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 400)
        status, payload = self.json_request(
            "POST", "/api/tap", body=b"definitely not json",
            headers={"Content-Type": "application/json"})
        self.assertEqual((status, "json" in payload["error"].lower()), (400, True))

    def test_out_of_panel_coordinates_get_400(self):
        status, _ = self.json_request(
            "POST", "/api/tap", body=b'{"x":600,"y":10}',
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertEqual(self.guest.taps, [])

    def test_valid_tap_reaches_guest(self):
        status, payload = self.json_request(
            "POST", "/api/tap", body=b'{"x":350,"y":760,"duration_ms":150}',
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["accepted"])
        self.assertEqual(self.guest.taps, [(350, 760, 150)])

    def test_guest_faults_map_to_503_json_not_crash(self):
        for error, attr in ((RuntimeError("Guest busy with another touch; retry shortly"), "tap_error"),
                            (BrokenPipeError("QMP closed"), "tap_error"),
                            (ConnectionResetError("QMP closed"), "reboot_error"),
                            (RuntimeError("QEMU process terminated"), "reboot_error")):
            with self.subTest(error=error):
                setattr(self.guest, attr, error)
                path = "/api/tap" if attr == "tap_error" else "/api/reboot"
                body = b'{"x":1,"y":1}' if attr == "tap_error" else None
                headers = {"Content-Type": "application/json"}
                status, payload = self.json_request("POST", path, body=body,
                                                    headers=headers)
                self.assertEqual(status, 503)
                self.assertIn(str(error), payload["error"])
                setattr(self.guest, attr, None)

    def test_state_failure_maps_to_503_on_both_read_endpoints(self):
        self.guest.state_error = BrokenPipeError("QMP closed")
        for path in ("/api/state", "/screen.png"):
            with self.subTest(path=path):
                status, payload = self.json_request("GET", path)
                self.assertEqual(status, 503)
                self.assertIn("QMP closed", payload["error"])

    def test_reboot_success_reports_reset(self):
        status, payload = self.json_request("POST", "/api/reboot")
        self.assertEqual(status, 200)
        self.assertEqual((payload["accepted"], self.guest.reboots), (True, 1))

    def test_unknown_post_path_gets_404(self):
        status, _ = self.json_request("POST", "/api/shell", body=b"{}")
        self.assertEqual(status, 404)


class HttpCachingTests(ServerHarnessMixin, unittest.TestCase):
    def test_state_json_contract_stable_plus_monotonic_and_frames(self):
        status, body = self.json_request("GET", "/api/state")
        self.assertEqual(status, 200)
        for key in ("frames", "nonwhite", "ready", "sha256",
                    "factory_guest", "physical_device", "rom",
                    "guest_booted", "boot_wait_seconds", "boot_failed"):
            self.assertIn(key, body)
        self.assertEqual(body["frames"], 3)
        self.assertIsInstance(body["monotonic_ms"], int)
        self.assertEqual(body["rom"]["name"], "factory.bin")
        self.assertEqual(body["rom"]["sha256"], FACTORY_ROM_SHA)
        self.assertTrue(body["rom"]["known_factory_image"])

    def test_state_reports_a_healthy_boot(self):
        status, body = self.json_request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(body["guest_booted"])
        self.assertFalse(body["boot_failed"])

    def test_state_reports_a_rom_that_never_produced_a_frame(self):
        """A ROM that does not boot must be visible as such, not as 'loading'."""
        self.guest.frames = 0
        self.guest.boot_wait_seconds = 60
        self.guest.boot_failed = True
        status, body = self.json_request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertFalse(body["guest_booted"])
        self.assertTrue(body["boot_failed"])
        self.assertEqual(body["boot_wait_seconds"], 60)
        self.assertFalse(body["ready"])

    def test_screen_png_advertises_validators(self):
        status, headers, body = self.full_request("GET", "/screen.png")
        self.assertEqual((status, body), (200, self.guest.png))
        self.assertEqual(headers["ETag"], self.guest.PNG_ETAG)
        self.assertEqual(headers["Last-Modified"],
                         email.utils.formatdate(self.guest.PNG_MTIME, usegmt=True))
        self.assertEqual(headers["Cache-Control"],
                         "private, max-age=0, must-revalidate")

    def test_if_none_match_returns_304_without_body(self):
        for condition in (self.guest.PNG_ETAG, "*", "W/" + self.guest.PNG_ETAG):
            with self.subTest(condition=condition):
                status, headers, body = self.full_request(
                    "GET", "/screen.png",
                    headers={"If-None-Match": condition})
                self.assertEqual(status, 304)
                self.assertEqual(body, b"")
                self.assertNotIn("Content-Length", headers)
                self.assertNotIn("Content-Type", headers)
                self.assertEqual(headers["ETag"], self.guest.PNG_ETAG)

    def test_mismatched_etag_beats_if_modified_since(self):
        stamp = email.utils.formatdate(self.guest.PNG_MTIME, usegmt=True)
        status, headers, body = self.full_request(
            "GET", "/screen.png?frame=3",
            headers={"If-None-Match": '"stale-etag"', "If-Modified-Since": stamp})
        self.assertEqual(status, 200)
        self.assertEqual(body, self.guest.png)

    def test_if_modified_since_without_etag_falls_back(self):
        self.guest.png_etag = None
        stamp = email.utils.formatdate(self.guest.PNG_MTIME, usegmt=True)
        status, _, body = self.full_request("GET", "/screen.png",
                                            headers={"If-Modified-Since": stamp})
        self.assertEqual((status, body), (304, b""))
        older = email.utils.formatdate(self.guest.PNG_MTIME - 5, usegmt=True)
        status, _, _ = self.full_request("GET", "/screen.png",
                                         headers={"If-Modified-Since": older})
        self.assertEqual(status, 200)
        status, _, _ = self.full_request("GET", "/screen.png",
                                         headers={"If-Modified-Since": "nonsense"})
        self.assertEqual(status, 200)


class HttpTelemetryErrorTests(ServerHarnessMixin, unittest.TestCase):
    def test_telemetry_endpoint_returns_counters(self):
        status, body = self.json_request("GET", "/api/telemetry")
        self.assertEqual(status, 200)
        self.assertEqual(body["ybx-epd-busy-rise-edges"]["value"], 17)
        self.assertEqual(body["ybx-epd-busy-high-ms"]["value"], 350)
        self.assertFalse(body["frame-sha256"]["available"])
        self.assertIsNone(body["frame-sha256"]["value"])

    def test_telemetry_guest_missing_maps_to_typed_503(self):
        self.guest.telemetry_error = GuestMissingError("QEMU process terminated; "
                                                       "use Restart virtual reader")
        status, headers, body = self.full_request("GET", "/api/telemetry")
        self.assertEqual(status, 503)
        payload = json.loads(body)
        self.assertEqual(payload["type"], "guest-missing")
        self.assertIn("Restart virtual reader", payload["error"])
        self.assertNotIn("Retry-After", headers)

    def test_typed_tap_errors_map_by_kind(self):
        cases = (
            (GuestBusyError("Guest busy with another touch; retry shortly"),
             503, "guest-busy", "1"),
            (TapQueueFullError("Tap queue is full (4 waiting); retry shortly"),
             429, "guest-busy", "1"),
            (GuestMissingError("QEMU process terminated"),
             503, "guest-missing", None))
        for error, code, kind, retry in cases:
            with self.subTest(error=error):
                self.guest.tap_error = error
                status, headers, body = self.full_request(
                    "POST", "/api/tap", body=b'{"x":1,"y":1}',
                    headers={"Content-Type": "application/json"})
                self.assertEqual(status, code)
                payload = json.loads(body)
                self.assertEqual((payload["error"], payload["type"]),
                                 (str(error), kind))
                self.assertEqual(headers.get("Retry-After"), retry)
                self.guest.tap_error = None

    def test_reboot_guest_missing_maps_to_typed_503(self):
        self.guest.reboot_error = GuestMissingError("QEMU process terminated")
        status, _, body = self.full_request("POST", "/api/reboot")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["type"], "guest-missing")

    def test_bad_requests_are_typed_400(self):
        status, payload = self.json_request(
            "POST", "/api/tap", body=b"nope",
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["type"], "bad-request")

    def test_concurrent_taps_are_all_accepted_by_the_bridge(self):
        self.guest.tap_delay = 0.03
        results = []
        lock = threading.Lock()
        def tap():
            status, payload = self.json_request(
                "POST", "/api/tap", body=b'{"x":350,"y":760}',
                headers={"Content-Type": "application/json"})
            with lock:
                results.append((status, payload["accepted"]))
        threads = [threading.Thread(target=tap) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)
        self.assertEqual(results, [(200, True)] * 4)
        self.assertEqual(self.guest.taps, [(350, 760, 150)] * 4)


class RomListingTests(unittest.TestCase):
    """GET-level behaviour of FactoryX4Pro.roms() against a real folder."""

    def setUp(self):
        self.guest = rom_guest()
        self.addCleanup(shutil.rmtree, self.guest.guest_tmpdir,
                        ignore_errors=True)

    def test_sorted_regular_files_with_sizes_hashes_and_active_flag(self):
        sha_beta = make_rom_file(self.guest, "b.bin", b"beta")
        sha_alpha = make_rom_file(self.guest, "a.bin", b"alpha")
        make_rom_file(self.guest, "c.bin", b"gamma")
        self.guest.rom_name = "b.bin"
        (self.guest.rom_dir / "subdir").mkdir()
        outside = self.guest.guest_tmpdir / "outside.bin"
        outside.write_bytes(b"sneaky")
        (self.guest.rom_dir / "link.bin").symlink_to(outside)
        listing = self.guest.roms()
        self.assertEqual([r["name"] for r in listing["roms"]],
                         ["a.bin", "b.bin", "c.bin"])
        self.assertFalse(listing["truncated"])
        self.assertEqual(listing["active"], "b.bin")
        a, b, c = listing["roms"]
        self.assertEqual((a["size"], a["sha256"], a["active"]),
                         (5, sha_alpha, False))
        self.assertEqual((b["size"], b["sha256"], b["active"]),
                         (4, sha_beta, True))
        self.assertEqual((c["size"], c["sha256"], c["active"]),
                         (5, hashlib.sha256(b"gamma").hexdigest(), False))

    def test_entry_count_is_capped_and_truncation_is_reported(self):
        self.guest.ROM_LIST_LIMIT = 3
        for i in range(5):
            make_rom_file(self.guest, f"rom{i}.bin", bytes([i]))
        listing = self.guest.roms()
        self.assertTrue(listing["truncated"])
        self.assertEqual([r["name"] for r in listing["roms"]],
                         ["rom0.bin", "rom1.bin", "rom2.bin"])

    def test_oversized_file_is_listed_but_never_fabricated_as_hashed(self):
        self.guest.ROM_MAX_HASH_BYTES = 8
        make_rom_file(self.guest, "huge.bin", b"z" * 40)
        item = self.guest.roms()["roms"][0]
        self.assertIsNone(item["sha256"])
        self.assertIn("not hashed", item["error"])
        self.assertEqual(item["provenance"], "unhashed")
        self.assertEqual(item["size"], 40)

    def test_sha256_file_returns_none_over_the_cap(self):
        path = self.guest.rom_dir / "cap.bin"
        path.write_bytes(b"123456789")
        self.assertEqual(sha256_file(path, 9),
                         hashlib.sha256(b"123456789").hexdigest())
        self.assertIsNone(sha256_file(path, 8))


class RomSwitchTests(unittest.TestCase):
    """POST-level behaviour of FactoryX4Pro.switch_rom() with a fake process."""

    def setUp(self):
        self.guest = rom_guest()
        self.addCleanup(shutil.rmtree, self.guest.guest_tmpdir,
                        ignore_errors=True)
        self.active_rom = (self.guest.workdir / "rom"
                           / self.guest.ROM_FILENAME_IN_WORKDIR)
        self.active_rom.write_bytes(b"OLD-ROM")

    def test_switch_clears_gt911_restarts_guest_and_resets_frame_cache(self):
        sha = make_rom_file(self.guest, "custom.bin", b"MY-OWN-ROM")
        result = self.guest.switch_rom("custom.bin")
        calls = self.guest.qmp.call_args_list
        self.assertEqual([c.args[0] for c in calls],
                         ["qom-set", "qom-set", "qom-set"])
        self.assertEqual([c.args[1]["property"] for c in calls],
                         ["ybx-touch-state", "ybx-touch-x", "ybx-touch-y"])
        self.assertTrue(all(c.args[1]["value"] == 0 for c in calls))
        self.guest._stop_process.assert_called_once_with()
        self.guest._launch.assert_called_once_with()
        self.assertEqual(self.active_rom.read_bytes(), b"MY-OWN-ROM")
        self.assertFalse((self.active_rom.parent /
                          (self.guest.ROM_FILENAME_IN_WORKDIR + ".new"))
                         .exists())
        self.assertEqual(self.guest.frame, -1)
        self.assertEqual(self.guest.sha, "")
        self.assertEqual(self.guest.png, self.guest.blank_png)
        self.assertEqual(self.guest.png_etag, '"blank"')
        self.assertEqual(self.guest.rom_name, "custom.bin")
        self.assertEqual(self.guest.rom_sha, sha)
        self.assertEqual(result["name"], "custom.bin")
        self.assertEqual(result["sha256"], sha)
        self.assertFalse(self.guest.tap_lock.locked())
        for _ in range(self.guest.tap_queue_depth):
            self.assertTrue(self.guest.tap_slots.acquire(blocking=False))

    def test_switch_of_dead_guest_still_swaps_rom_and_relaunches(self):
        make_rom_file(self.guest, "custom.bin", b"NEW")
        self.guest.process = Mock(name="process", poll=Mock(return_value=1))
        self.guest.switch_rom("custom.bin")
        self.guest.qmp.assert_not_called()
        self.guest._launch.assert_called_once_with()
        self.assertEqual(self.active_rom.read_bytes(), b"NEW")

    def test_traversal_absolute_and_nested_names_are_rejected(self):
        (self.guest.rom_dir / "sub").mkdir()
        (self.guest.rom_dir / "sub" / "ok.bin").write_bytes(b"x")
        for name in ("../evil.bin", "/etc/passwd", "sub/ok.bin", "..", ".",
                     "./ok.bin", "a\\b.bin", "", "..\\..\\x", "\x00.bin"):
            with self.subTest(name=name):
                with self.assertRaises(BadRequestError) as caught:
                    self.guest.switch_rom(name)
                self.assertEqual(caught.exception.http_status, 400)
        self.guest._launch.assert_not_called()
        self.assertEqual(self.active_rom.read_bytes(), b"OLD-ROM")

    def test_symlink_to_outside_file_is_rejected(self):
        outside = self.guest.guest_tmpdir / "outside.bin"
        outside.write_bytes(b"escape")
        (self.guest.rom_dir / "link.bin").symlink_to(outside)
        with self.assertRaises(BadRequestError) as caught:
            self.guest.switch_rom("link.bin")
        self.assertIn("regular file", str(caught.exception))
        self.guest._launch.assert_not_called()

    def test_directory_and_unknown_name_are_typed_4xx(self):
        (self.guest.rom_dir / "adir.bin").mkdir()
        with self.assertRaises(BadRequestError):
            self.guest.switch_rom("adir.bin")
        with self.assertRaises(RomNotFoundError) as caught:
            self.guest.switch_rom("ghost.bin")
        self.assertEqual(caught.exception.http_status, 404)
        self.assertEqual(caught.exception.kind, "rom-not-found")
        self.guest._launch.assert_not_called()

    def test_oversized_rom_is_refused_without_replacing_the_active_one(self):
        self.guest.ROM_MAX_HASH_BYTES = 8
        make_rom_file(self.guest, "huge.bin", b"z" * 40)
        with self.assertRaises(BadRequestError) as caught:
            self.guest.switch_rom("huge.bin")
        self.assertIn("larger", str(caught.exception))
        self.guest._launch.assert_not_called()
        self.assertEqual(self.active_rom.read_bytes(), b"OLD-ROM")

    def test_failed_relaunch_leaves_swap_visible_and_locks_free(self):
        make_rom_file(self.guest, "junk.bin", b"not-a-rom")
        self.guest._launch = Mock(side_effect=ConnectionRefusedError("qmp"))
        with self.assertRaises(OSError):
            self.guest.switch_rom("junk.bin")
        self.assertEqual(self.guest.frame, -1)
        self.assertEqual(self.guest.png, self.guest.blank_png)
        self.assertEqual(self.guest.rom_name, "junk.bin")
        self.assertFalse(self.guest.tap_lock.locked())
        for _ in range(self.guest.tap_queue_depth):
            self.assertTrue(self.guest.tap_slots.acquire(blocking=False))
        # A guest that never came up surfaces as guest-missing, never as a
        # boot claim: the dead process must be reported by /api/state.
        self.guest.process = Mock(name="process", poll=Mock(return_value=-9))
        with self.assertRaises(GuestMissingError):
            self.guest.state()

    def test_switch_works_when_no_qmp_client_was_ever_established(self):
        make_rom_file(self.guest, "custom.bin", b"NEW")
        self.guest.client = None
        result = self.guest.switch_rom("custom.bin")
        self.assertEqual(result["name"], "custom.bin")
        self.guest._launch.assert_called_once_with()
        self.assertEqual(self.active_rom.read_bytes(), b"NEW")

    def test_state_reports_active_rom_after_switch(self):
        qmp, _ = telemetry_qmp({"ybx-epd-frames": 0, "ybx-epd-nonwhite": 7})
        self.guest.qmp = qmp
        sha = make_rom_file(self.guest, "custom.bin", b"MY-OWN-ROM")
        self.guest.switch_rom("custom.bin")
        state = self.guest.state()
        self.assertEqual(state["rom"],
                         {"name": "custom.bin", "sha256": sha,
                          "provenance": "custom",
                          "provenance_note":
                              "Custom ROM of unknown provenance (SHA-256 "
                              "matches no known factory image)",
                          "known_factory_image": False})
        for key in ("frames", "nonwhite", "ready", "sha256",
                    "factory_guest", "physical_device"):
            self.assertIn(key, state)


class RomProvenanceTests(unittest.TestCase):
    def test_known_factory_images_are_recognized_and_others_are_custom(self):
        self.assertEqual(FactoryX4Pro.provenance_of(FACTORY_ROM_SHA),
                         "factory-rom")
        self.assertEqual(FactoryX4Pro.provenance_of(FACTORY_FLASH_SHA),
                         "factory-flash")
        self.assertEqual(FactoryX4Pro.provenance_of("f" * 64), "custom")
        self.assertEqual(FactoryX4Pro.provenance_of(None), "unhashed")
        self.assertEqual(KNOWN_FACTORY_IMAGES[FACTORY_ROM_SHA],
                         "factory-rom")
        self.assertEqual(KNOWN_FACTORY_IMAGES[FACTORY_FLASH_SHA],
                         "factory-flash")

    def test_factory_media_on_this_mac_match_the_pinned_constants(self):
        qa = Path.home() / ("Projects/YBX/artifacts/ybx-esp32-system-"
                            "emulation-20261029/qa-20261029")
        rom = qa / "s3-full-rom-generator-test.bin"
        flash = qa / "s3-flash-complete-rom-test.bin"
        if not (rom.is_file() and flash.is_file()):
            self.skipTest("factory QA media not present on this machine")
        self.assertEqual(sha256_file(rom, 64 * 1024 * 1024), FACTORY_ROM_SHA)
        self.assertEqual(sha256_file(flash, 64 * 1024 * 1024),
                         FACTORY_FLASH_SHA)

    def test_page_shows_provenance_and_degrades_without_the_rom_api(self):
        self.assertIn("/api/roms", PAGE)
        self.assertIn("/api/rom", PAGE)
        self.assertIn("Load ROM", PAGE)
        self.assertIn("showRomActive(s.rom)", PAGE)
        self.assertIn("provenance_note", PAGE)
        self.assertIn("ROM list unavailable", PAGE)
        self.assertNotIn("Original factory firmware", PAGE)
        # Option labels use textContent, never innerHTML, for hostile names.
        self.assertIn("o.textContent=item.name", PAGE)


class HttpRomContractTests(ServerHarnessMixin, unittest.TestCase):
    """Drive the /api/roms and /api/rom endpoints through the real socket."""

    def json_post(self, body):
        return self.json_request("POST", "/api/rom", body=body,
                                 headers={"Content-Type": "application/json"})

    def test_roms_endpoint_lists_candidates_with_active_flag(self):
        status, body = self.json_request("GET", "/api/roms")
        self.assertEqual(status, 200)
        self.assertEqual([r["name"] for r in body["roms"]],
                         ["a-custom.bin", "factory.bin"])
        self.assertEqual(body["active"], "factory.bin")
        self.assertTrue(body["roms"][1]["active"])
        self.assertFalse(body["roms"][0]["active"])

    def test_roms_endpoint_failure_is_503_not_an_empty_list(self):
        self.guest.rom_list_error = OSError("[Errno 2] No such directory")
        status, body = self.json_request("GET", "/api/roms")
        self.assertEqual(status, 503)
        self.assertIn("No such directory", body["error"])

    def test_switch_accepts_a_known_rom_and_reports_its_sha(self):
        status, body = self.json_post(b'{"name": "a-custom.bin"}')
        self.assertEqual(status, 200)
        self.assertTrue(body["accepted"])
        self.assertEqual((body["name"], body["sha256"], body["provenance"]),
                         ("a-custom.bin", "c" * 64, "custom"))
        self.assertIn("unknown provenance", body["provenance_note"])
        self.assertEqual(self.guest.rom_switches, ["a-custom.bin"])
        # The emulated session reset like /api/reboot does.
        self.assertEqual(self.guest.png_etag, '"blank"')
        status, state = self.json_request("GET", "/api/state")
        self.assertEqual(state["rom"]["name"], "a-custom.bin")
        self.assertFalse(state["rom"]["known_factory_image"])

    def test_unknown_rom_is_typed_404_and_known_factory_keeps_factory_flag(self):
        status, body = self.json_post(b'{"name": "ghost.bin"}')
        self.assertEqual(status, 404)
        self.assertEqual(body["type"], "rom-not-found")
        status, body = self.json_post(b'{"name": "factory.bin"}')
        self.assertEqual(status, 200)
        self.assertEqual(body["provenance"], "factory-rom")
        self.assertTrue(body["accepted"])

    def test_invalid_switch_requests_are_typed_400(self):
        self.guest.rom_error = BadRequestError("ROM name must be a plain "
                                               "file name inside --rom-dir")
        for body, want_type in (
                (b'not json', "bad-request"),
                (b'[1]', "bad-request"),
                (b'{"name": 5}', "bad-request"),
                (b'{"name": "../x"}', "bad-request")):
            with self.subTest(body=body):
                status, payload = self.json_post(body)
                self.assertEqual(status, 400)
                self.assertEqual(payload["type"], want_type)
        self.guest.rom_error = None
        for method, headers, body in (
                ("POST", {"Content-Type": "text/plain"}, b'{"name":"a"}'),
                ("POST", {"Content-Type": "application/json"}, b"")):
            with self.subTest(headers=headers):
                status, payload = self.json_post(body) if body == b"" else \
                    self.json_request("POST", "/api/rom", body=body,
                                      headers=headers)
                self.assertEqual(status, 400)
                self.assertEqual(payload["type"], "bad-request")

    def test_switch_guest_fault_maps_to_typed_503(self):
        self.guest.rom_error = GuestMissingError("QEMU process terminated; "
                                                 "use Restart virtual reader")
        status, body = self.json_post(b'{"name": "a-custom.bin"}')
        self.assertEqual(status, 503)
        self.assertEqual(body["type"], "guest-missing")
        self.assertEqual(self.guest.rom_switches, [])

    def test_switch_resets_cached_frame_like_reboot_endpoint(self):
        status, _ = self.json_post(b'{"name": "a-custom.bin"}')
        self.assertEqual(status, 200)
        status, headers, body = self.full_request(
            "GET", "/screen.png", headers={"If-None-Match": FakeGuest.PNG_ETAG})
        self.assertEqual(status, 200)
        self.assertEqual(headers["ETag"], '"blank"')


class LauncherBindGuardTests(unittest.TestCase):
    """start_x4pro_browser.sh must reject public binds and shout on LAN binds.

    Runs the real launcher with prepared-but-fileless roots so it exits at
    the missing-artifact check: the Docker CLI is never reached.
    """

    HERE = Path(__file__).resolve().parent

    def run_launcher(self, bind):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "emptybin").mkdir()
            qemu = root / "qemu" / "build-ybx"
            qemu.mkdir(parents=True)
            qa = root / "qa"
            qa.mkdir()
            env = {**os.environ,
                   "PATH": str(root / "emptybin") + os.pathsep + os.environ["PATH"],
                   "YBX_QEMU_ROOT": str(qemu.parent),
                   "YBX_QA_DIR": str(qa),
                   "YBX_BROWSER_PORT": "8798",
                   "YBX_BROWSER_BIND_ADDRESS": bind}
            result = subprocess.run(["bash", str(self.HERE / "start_x4pro_browser.sh")],
                                    env=env, text=True, capture_output=True,
                                    timeout=8)
            return result

    def test_public_bind_rejected_without_warning(self):
        result = self.run_launcher("8.8.8.8")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Refusing non-private", result.stderr)
        self.assertNotIn("WARNING: exposing", result.stderr)

    def test_link_local_bind_rejected(self):
        result = self.run_launcher("169.254.1.1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Refusing non-private", result.stderr)

    def test_lan_bind_warns_loudly_before_doing_anything(self):
        result = self.run_launcher("192.168.1.50")
        self.assertEqual(result.returncode, 2)  # exits later on missing files
        self.assertIn("WARNING: exposing the YBX browser bridge", result.stderr)
        self.assertIn("NO authentication", result.stderr)
        self.assertIn("ssh -L 8798:127.0.0.1:8798", result.stderr)
        self.assertNotIn("Refusing non-private", result.stderr)

    def test_loopback_bind_stays_quiet(self):
        result = self.run_launcher("127.0.0.1")
        self.assertEqual(result.returncode, 2)  # missing artifacts, as prepared
        self.assertNotIn("WARNING: exposing", result.stderr)
        self.assertNotIn("Refusing", result.stderr)


FACTORY_FLASH_SHA = ("206ed9661bd71477d1a54397c5a2cfe490179a3f59ab094f1e742"
                     "1745c609523")


def make_flash_image(path, size=0x20000, valid=True):
    """Minimal ESP flash image; valid=False leaves it without the app layout."""
    data = bytearray(b"\xff" * size)
    data[0] = 0xE9
    if valid:
        data[0x8000:0x8002] = b"\xaa\x50"
        data[0x10000] = 0xE9
    Path(path).write_bytes(bytes(data))
    return hashlib.sha256(bytes(data)).hexdigest()


def make_sd_image(path, size=0x400):
    data = bytearray(b"\x00" * size)
    data[0x1FE:0x200] = b"\x55\xaa"
    Path(path).write_bytes(bytes(data))
    return hashlib.sha256(bytes(data)).hexdigest()


def firmware_guest(**overrides):
    """browser_guest plus a real firmware folder and work dir on disk."""
    guest = browser_guest()
    guest.qmp = Mock(return_value={"return": 4})
    guest.frame = 4
    root = Path(tempfile.mkdtemp(prefix="ybx-x4pro-fw-"))
    guest.guest_tmpdir = root
    guest.workdir = root / "work"
    guest.workdir.mkdir(parents=True)
    guest.firmware_dir = root / "firmware"
    guest.firmware_dir.mkdir()
    guest.firmware_name = "factory.bin"
    guest.firmware_sha = FACTORY_FLASH_SHA
    guest.firmware_path = Path(tempfile.mkdtemp(prefix="ybx-x4pro-fwpath-")) \
        / "factory.bin"
    guest.firmware_path.write_bytes(b"\xe9" + b"\xff" * 0x20000)
    guest.boot_started_at = time.monotonic()
    guest.rom_dir = root / "roms"
    guest.rom_dir.mkdir()
    guest.rom_name = "factory.bin"
    guest.rom_sha = FACTORY_ROM_SHA
    for key, value in overrides.items():
        setattr(guest, key, value)
    return guest


class FirmwarePickerTests(unittest.TestCase):
    def setUp(self):
        self.guest = firmware_guest()
        self.flash_sha = make_flash_image(
            self.guest.firmware_dir / "own-build.bin")
        self.sd_sha = make_sd_image(self.guest.firmware_dir / "own-build.img")
        make_flash_image(self.guest.firmware_dir / "broken.bin", valid=False)
        (self.guest.firmware_dir / "app-only.bin").write_bytes(b"\x73" * 32)

    def test_listing_reports_kind_size_and_hash(self):
        listing = self.guest.firmwares()
        by_name = {item["name"]: item for item in listing["firmwares"]}
        self.assertEqual(by_name["own-build.bin"]["kind"], "flash")
        self.assertEqual(by_name["own-build.bin"]["sha256"], self.flash_sha)
        self.assertEqual(by_name["own-build.img"]["kind"], "sd")
        self.assertEqual(by_name["app-only.bin"]["kind"], "unknown")
        self.assertEqual(by_name["broken.bin"]["kind"], "unknown")
        self.assertFalse(by_name["own-build.bin"]["active"])

    def test_switch_installs_the_image_and_restarts_the_guest(self):
        self.guest.frame = 42
        self.guest.sha = "old-sha"
        result = self.guest.switch_firmware("own-build.bin")
        self.assertEqual(result["name"], "own-build.bin")
        self.assertEqual(result["sha256"], self.flash_sha)
        self.assertEqual(result["kind"], "flash")
        self.assertEqual(result["provenance"], "custom")
        self.assertEqual(self.guest.firmware_name, "own-build.bin")
        self.assertEqual(self.guest.firmware_sha, self.flash_sha)
        installed = (self.guest.workdir / "flash.bin").read_bytes()
        self.assertEqual(installed[0], 0xE9)
        self.assertEqual(self.guest.frame, -1)     # frame cache was reset
        self.assertEqual(self.guest.sha, "")
        self.guest._launch.assert_called_once()

    def test_switch_copies_the_paired_sd_image(self):
        self.guest.switch_firmware("own-build.bin")
        self.assertTrue((self.guest.workdir / "sd.img").is_file())

    def test_a_file_that_is_not_a_flash_image_is_refused(self):
        with self.assertRaises(BadRequestError):
            self.guest.switch_firmware("app-only.bin")
        with self.assertRaises(BadRequestError):
            self.guest.switch_firmware("broken.bin")
        self.guest._launch.assert_not_called()

    def test_traversal_and_unknown_names_are_refused(self):
        for name in ("../outside.bin", "/etc/passwd", "sub/dir.bin"):
            with self.subTest(name=name):
                with self.assertRaises(BadRequestError):
                    self.guest.switch_firmware(name)
        with self.assertRaises(RomNotFoundError):
            self.guest.switch_firmware("missing.bin")

    def test_state_reports_the_active_firmware_and_its_provenance(self):
        state = self.guest.state()
        self.assertEqual(state["firmware"]["name"], "factory.bin")
        self.assertTrue(state["firmware"]["known_factory_image"])
        self.assertTrue(state["factory_guest"])
        self.guest.switch_firmware("own-build.bin")
        self.guest.frame = 4   # keep the fake guest out of the frame-cache path
        state = self.guest.state()
        self.assertEqual(state["firmware"]["name"], "own-build.bin")
        self.assertFalse(state["firmware"]["known_factory_image"])
        self.assertFalse(state["factory_guest"],
                         "a custom build must not be presented as factory firmware")


class FirmwareHttpContractTests(ServerHarnessMixin):
    def setUp(self):
        super().setUp()
        self.guest.firmware_items = [
            {"name": "own-build.bin", "size": 131072, "sha256": "a" * 64,
             "kind": "flash", "active": False, "provenance": "custom",
             "provenance_note": "Custom ROM of unknown provenance"},
            {"name": "factory.bin", "size": 16777216,
             "sha256": FACTORY_FLASH_SHA, "kind": "flash", "active": True,
             "provenance": "factory-flash",
             "provenance_note": "Factory flash image"},
        ]
        self.guest.firmware_switches = []

    def test_firmware_list_is_served(self):
        status, body = self.json_request("GET", "/api/firmware")
        self.assertEqual(status, 200)
        self.assertEqual(body["active"], "factory.bin")
        self.assertEqual(body["firmwares"][0]["name"], "own-build.bin")
        self.assertEqual(body["firmwares"][0]["kind"], "flash")

    def test_firmware_switch_is_accepted(self):
        status, body = self.json_request(
            "POST", "/api/firmware",
            body=json.dumps({"name": "own-build.bin"}),
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertTrue(body["accepted"])
        self.assertEqual(body["action"], "firmware-switch")

    def test_unknown_firmware_name_is_a_typed_error(self):
        status, body = self.json_request(
            "POST", "/api/firmware",
            body=json.dumps({"name": "missing.bin"}),
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_traversal_in_firmware_name_is_refused(self):
        status, body = self.json_request(
            "POST", "/api/firmware",
            body=json.dumps({"name": "../outside.bin"}),
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertIn("plain file name", body["error"])
