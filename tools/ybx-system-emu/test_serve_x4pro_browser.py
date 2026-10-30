"""Host-only guardrails for the X4 Pro QEMU browser click bridge."""
import http.client
import json
import threading
import unittest
from unittest.mock import Mock

from serve_x4pro_browser import (FactoryX4Pro, check_touch,
                                 guest_ready_for_touch, make_handler, W, H,
                                 PAGE)
from http.server import ThreadingHTTPServer


def browser_guest(**overrides):
    """FactoryX4Pro instance without a real QEMU process or socket."""
    guest = FactoryX4Pro.__new__(FactoryX4Pro)
    guest.lock = threading.RLock()
    guest.tap_lock = threading.Lock()
    guest.process = Mock(name="process", poll=Mock(return_value=None))
    guest.client = Mock()
    guest.frame = 42
    guest.sha = "old-sha"
    guest.png = b"old-frame"
    guest.blank_png = b"blank-frame"
    guest.qmp = Mock(return_value={"return": {}})
    guest._launch = Mock()
    guest._stop_process = Mock()
    for key, value in overrides.items():
        setattr(guest, key, value)
    return guest


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
        self.assertFalse(guest.tap_lock.locked())

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
            with self.assertRaises(RuntimeError) as caught:
                guest.tap(1, 2, 60)
        finally:
            self.assertTrue(guest.tap_lock.locked())
            guest.tap_lock.release()
        self.assertIn("busy", str(caught.exception))

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


class FakeGuest:
    def __init__(self):
        self.taps = []
        self.reboots = 0
        self.png = b"\x89PNG fake"
        self.state_error = None
        self.tap_error = None
        self.reboot_error = None

    def state(self):
        if self.state_error:
            raise self.state_error
        return {"frames": 3, "nonwhite": 1234, "ready": True,
                "sha256": "0" * 64, "factory_guest": True,
                "physical_device": False}

    def tap(self, x, y, ms):
        if self.tap_error:
            raise self.tap_error
        self.taps.append((x, y, ms))

    def reboot(self):
        if self.reboot_error:
            raise self.reboot_error
        self.reboots += 1


class HttpContractTests(unittest.TestCase):
    """Drive make_handler through a real socket with an in-process guest."""

    def setUp(self):
        self.guest = FakeGuest()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          make_handler(self.guest))
        self.thread = threading.Thread(target=self.server.serve_forever,
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
