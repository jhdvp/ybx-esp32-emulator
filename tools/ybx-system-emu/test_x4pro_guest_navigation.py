"""Host tests for multi-step X4 Pro guest navigation acceptance logic.

No QEMU, no network, no shared browser emulator. Frames are either built
byte-by-byte in the test (clearly synthetic) or, when the recorded genuine
guest captures are available, read from them through YBX_GUEST_EVIDENCE_DIR.
"""
import hashlib
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from probe_x4pro_guest_touch_ui import qemu_command, x4pro_raw_to_portrait
import x4pro_guest_navigation as nav

BLANK = b"\xff" * nav.FRAME_BYTES


def paint(frame: bytearray, x0: int, y0: int, x1: int, y1: int) -> None:
    """Set a rectangle to black ink in the portrait-packed buffer."""
    for y in range(y0, y1):
        for x in range(x0, x1):
            frame[y * nav.PORTRAIT_STRIDE + x // 8] &= ~(0x80 >> (x % 8))


def home_frame(with_dialog=False, with_more_tab=False):
    frame = bytearray(BLANK)
    paint(frame, *nav.BAND_HOME_HEADER[:2], nav.BAND_HOME_HEADER[2],
          nav.BAND_HOME_HEADER[3])
    if with_more_tab:
        paint(frame, 246, 741, 453, 797)
    if with_dialog:
        paint(frame, 56, 239, 417, 241)
        paint(frame, 56, 558, 417, 560)
    return bytes(frame)


def ybx_settings_frame():
    frame = bytearray(BLANK)
    paint(frame, 16, 670, 444, 693)
    return bytes(frame)


def system_settings_frame():
    frame = bytearray(BLANK)
    paint(frame, 65, 508, 175, 534)
    return bytes(frame)


def packed_portrait_from_png(path):
    """Return the portrait-packed 1bpp buffer stored in a save_mono_png file.

    save_mono_png writes the buffer verbatim (one 0x00 filter byte per row),
    so the scanlines are the framebuffer itself: a cleared bit is black ink.
    """
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset, idat, header = 8, b"", None
    while offset < len(data):
        size = struct.unpack(">I", data[offset:offset + 4])[0]
        kind, body = data[offset + 4:offset + 8], data[offset + 8:offset + 8 + size]
        offset += 12 + size
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
    width, height, depth, color, *_ = header
    assert (depth, color) == (1, 0), (depth, color)
    raw = zlib.decompress(idat)
    stride = width // 8
    assert len(raw) == height * (stride + 1)
    rows = [raw[row * (stride + 1):row * (stride + 1) + stride] for row in range(height)]
    assert all(raw[row * (stride + 1)] == 0 for row in range(height)), "filter byte"
    return width, height, b"".join(rows)


class TargetGeometryTests(unittest.TestCase):
    def test_targets_inside_sensor_bounds(self):
        for name, target in nav.TARGETS.items():
            x, y = target["raw_xy"]
            self.assertTrue(0 <= x <= 479 and 0 <= y <= 799, name)

    def test_raw_coordinates_equal_portrait_coordinates(self):
        # The X4 Pro swap/flip and the portrait rotation cancel each other.
        for name, target in nav.TARGETS.items():
            x, y = target["raw_xy"]
            self.assertEqual(x4pro_raw_to_portrait(x, y), (x, y), name)

    def test_dialog_targets_land_inside_the_measured_dialog(self):
        left, top, right, bottom = nav.DIALOG_RECT
        for name in ("more-settings",):
            x, y = nav.TARGETS[name]["raw_xy"]
            self.assertTrue(left < x < right and top < y < bottom, name)

    def test_dismiss_target_avoids_dialog_and_nav_bar(self):
        left, top, right, bottom = nav.DIALOG_RECT
        x, y = nav.TARGETS["more-dismiss"]["raw_xy"]
        self.assertGreater(y, bottom)
        self.assertLess(y, 741)  # measured "More" tab pill top edge

    def test_back_target_inside_measured_header_arrow(self):
        x, y = nav.TARGETS["system-settings-back"]["raw_xy"]
        self.assertTrue(24 <= x <= 108 and 54 <= y <= 73, (x, y))

    def test_sequences_reference_known_targets(self):
        for sequence, steps in nav.SEQUENCES.items():
            for step in steps:
                self.assertIn(step["target"], nav.TARGETS, sequence)
                self.assertIn(step["expect_before"], {"home", "more-dialog",
                                                      "ybx-settings", "system-settings"})


class ScreenPredicateTests(unittest.TestCase):
    def test_blank_panel_is_unknown(self):
        self.assertEqual(nav.screen_state(BLANK), "unknown")
        self.assertFalse(nav.home_screen_visible(BLANK))
        self.assertFalse(nav.more_dialog_visible(BLANK))

    def test_home_and_more_dialog_are_distinguished(self):
        self.assertEqual(nav.screen_state(home_frame()), "home")
        self.assertEqual(nav.screen_state(home_frame(with_more_tab=True)), "home")
        self.assertEqual(nav.screen_state(home_frame(with_dialog=True)), "more-dialog")

    def test_settings_screens_are_distinguished(self):
        self.assertEqual(nav.screen_state(ybx_settings_frame()), "ybx-settings")
        self.assertEqual(nav.screen_state(system_settings_frame()), "system-settings")

    def test_partial_dialog_border_is_not_a_dialog(self):
        frame = bytearray(BLANK)
        paint(frame, 56, 239, 200, 241)   # half of the top border only
        paint(frame, 56, 558, 417, 560)
        self.assertFalse(nav.more_dialog_visible(bytes(frame)))

    def test_frame_helpers_validate_input(self):
        with self.assertRaises(ValueError):
            nav.is_dark(b"\xff" * 10, 0, 0)
        with self.assertRaises(ValueError):
            nav.is_dark(BLANK, nav.PORTRAIT_WIDTH, 0)

    def test_dark_count_and_run_measure_ink(self):
        frame = bytearray(BLANK)
        paint(frame, 100, 300, 140, 305)
        self.assertEqual(nav.dark_count(bytes(frame), (0, 0, 480, 800)), 40 * 5)
        self.assertEqual(nav.longest_dark_run(bytes(frame), (0, 0, 480, 800)), 40)


class StepGateTests(unittest.TestCase):
    def setUp(self):
        self.step = {"name": "open-settings", "target": "more-settings",
                     "expect_before": "more-dialog", "expect_after": "ybx-settings"}
        self.before = {"frames": 5, "frame_sha256": "more",
                       "screen_state": "more-dialog"}
        self.after = {"frames": 6, "frame_sha256": "settings",
                      "screen_state": "ybx-settings"}
        self.touch_before = {"point-reads": 1, "status-clears": 2}
        self.touch_after = {"point-reads": 2, "status-clears": 4}
        self.injections = {"set-x": {"ok": True}, "set-y": {"ok": True},
                           "press": {"ok": True}, "release": {"ok": True}}

    def assess(self):
        return nav.assess_step(self.step, self.before, self.after,
                               self.touch_before, self.touch_after,
                               self.injections)

    def test_all_gates_confirmed(self):
        result = self.assess()
        self.assertTrue(result["passed"], result["failure_codes"])
        self.assertEqual(result["raw_xy"], [240, 453])

    def test_qmp_success_without_guest_i2c_rejected(self):
        self.touch_after = {"point-reads": 1, "status-clears": 2}
        self.assertEqual(self.assess()["failure_codes"],
                         ["guest_did_not_read_gt911_point",
                          "guest_did_not_clear_press_and_release"])

    def test_missing_release_ack_rejected(self):
        self.touch_after["status-clears"] = 3
        self.assertIn("guest_did_not_clear_press_and_release",
                      self.assess()["failure_codes"])

    def test_rejected_injection_rejected(self):
        self.injections["press"] = {"ok": False}
        self.assertIn("qmp_injection_rejected", self.assess()["failure_codes"])

    def test_pixels_unchanged_rejected(self):
        self.after["frame_sha256"] = "more"
        self.assertIn("panel_pixels_unchanged", self.assess()["failure_codes"])

    def test_no_new_refresh_rejected(self):
        self.after["frames"] = 5
        self.assertIn("epd_refresh_count_not_increased", self.assess()["failure_codes"])

    def test_missing_readout_rejected(self):
        self.after["frame_sha256"] = None
        self.assertIn("missing_visible_frame_readout", self.assess()["failure_codes"])

    def test_unrecognised_screen_is_never_a_success(self):
        self.after["screen_state"] = "unknown"
        codes = self.assess()["failure_codes"]
        self.assertIn("target_screen_is_unknown", codes)

    def test_wrong_starting_screen_rejected(self):
        self.before["screen_state"] = "home"
        self.assertIn("precondition_screen_is_home", self.assess()["failure_codes"])

    def test_wrong_destination_screen_rejected(self):
        self.after["screen_state"] = "system-settings"
        self.assertIn("target_screen_is_system-settings", self.assess()["failure_codes"])


class SequenceVerdictTests(unittest.TestCase):
    def step_result(self, name, passed, sha, screen):
        return {"step": name, "target": name, "passed": passed,
                "failure_codes": [] if passed else ["panel_pixels_unchanged"],
                "frame_sha256_after": sha, "screen_after": screen,
                "screen_before": "home", "expected_screen_after": screen,
                "expected_screen_before": "home", "raw_xy": [0, 0]}

    def test_partial_sequence_never_claims_navigation(self):
        results = [self.step_result("open-more", True, "a", "more-dialog"),
                   self.step_result("open-settings", False, "a", "more-dialog")]
        verdict = nav.assess_sequence(results)
        self.assertFalse(verdict["navigation_sequence_verified"])
        self.assertEqual(verdict["first_failing_step"], "open-settings")
        self.assertEqual(verdict["reached_screen"], "more-dialog")
        self.assertEqual(verdict["frame_sha256_chain"], ["a", "a"])

    def test_full_sequence_verified(self):
        results = [self.step_result("open-more", True, "a", "more-dialog"),
                   self.step_result("open-settings", True, "b", "ybx-settings")]
        verdict = nav.assess_sequence(results)
        self.assertTrue(verdict["navigation_sequence_verified"])
        self.assertIsNone(verdict["first_failing_step"])

    def test_empty_run_is_not_verified(self):
        self.assertFalse(nav.assess_sequence([])["navigation_sequence_verified"])

    def test_diagnostics_follow_step_order(self):
        results = [self.step_result("open-more", False, "a", "unknown"),
                   self.step_result("open-settings", True, "b", "ybx-settings")]
        diagnostics = nav.failure_diagnostics(results)
        self.assertEqual([d["step"] for d in diagnostics], ["open-more"])
        self.assertEqual(diagnostics[0]["codes"], ["panel_pixels_unchanged"])
        self.assertEqual(diagnostics[0]["screen_observed"], "unknown")

    def test_replay_signal_reports_identical_repaint(self):
        results = [self.step_result("open-settings", True, "b", "ybx-settings"),
                   self.step_result("back-to-settings", True, "b", "ybx-settings")]
        signal = nav.panel_replay_signal(results, "open-settings", "back-to-settings")
        self.assertTrue(signal["restores_previous_frame"])
        self.assertEqual(signal["reference_sha256"], "b")

    def test_replay_signal_without_reference_is_unavailable(self):
        results = [self.step_result("back-to-settings", True, "b", "ybx-settings")]
        signal = nav.panel_replay_signal(results, "open-settings", "back-to-settings")
        self.assertFalse(signal["available"])

    def test_resolve_sequence_rejects_unknown_name(self):
        with self.assertRaises(ValueError):
            nav.resolve_sequence("settings-to-bootloader")

    def test_resolve_sequence_returns_a_copy(self):
        first = nav.resolve_sequence("more-to-settings-and-back")
        first[0]["target"] = "tampered"
        self.assertEqual(nav.SEQUENCES["more-to-settings-and-back"][0]["target"],
                         "home-more")


class QemuLaunchContractTests(unittest.TestCase):
    """The runner must launch its own guest with every opt-in X4 Pro model."""

    def command(self, **kwargs):
        return qemu_command(Path("/bin/qemu-system-esp32s3"), Path("/tmp/work"),
                            Path("/tmp/work/qmp.sock"), Path("/tmp/work/rom"),
                            **kwargs)

    def test_opt_in_models_and_qmp_socket(self):
        cmd = self.command()
        machine = cmd[cmd.index("-machine") + 1]
        for flag in ("ybx-x4pro-i2c", "ybx-x4pro-gt911", "ybx-x4pro-sd-power",
                     "ybx-x4pro-spi2", "ybx-x4pro-epd"):
            self.assertIn(f"{flag}=on", machine)
        self.assertIn("-nographic", cmd)
        self.assertNotIn("-display", cmd)
        self.assertIn("unix:/tmp/work/qmp.sock,server=on,wait=off", cmd)
        self.assertTrue(any("factory-flash.bin" in part for part in cmd))
        self.assertTrue(any(part.startswith("file=") and "sd.img,if=sd" in part
                            for part in cmd))

    def test_gdb_options(self):
        self.assertIn("tcp:0.0.0.0:25997", self.command(gdb_port=25997))
        self.assertIn("-S", self.command(gdb_port=25997, gdb_start_paused=True))
        with self.assertRaises(ValueError):
            self.command(gdb_port=80)
        with self.assertRaises(ValueError):
            self.command(gdb_start_paused=True)


class CapturePlumbingTests(unittest.TestCase):
    """run_x4pro_guest_navigation.capture() against a stubbed QMP reader."""

    def setUp(self):
        import run_x4pro_guest_navigation as runner
        self.runner = runner
        self.landscape = b"\xff" * (800 * 480 // 8)
        self.counters = {"frames": 5, "nonwhite": 7274, "ram-writes": 480000,
                         "commands": 99, "busy": 0}

    def qmp(self, command, arguments=None):
        if command == "qom-get":
            prop = arguments["property"]
            if prop == "ybx-epd-visible-hex":
                return {"return": self.landscape.hex()}
            if prop.startswith("ybx-epd-") and prop[8:] in self.counters:
                return {"return": self.counters[prop[8:]]}
        return {"error": "unsupported"}

    def test_reads_counters_sha_png_and_screen_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            state = self.runner.capture(self.qmp, "step1a", out, "nav")
            self.assertEqual(state["frames"], 5)
            self.assertEqual(state["nonwhite"], 7274)
            self.assertEqual(state["frame_sha256"],
                             hashlib.sha256(self.landscape).hexdigest())
            self.assertEqual(state["screen_state"], "unknown")
            self.assertFalse(state["more_dialog_visible"])
            png = Path(state["png"])
            self.assertTrue(png.exists())
            header = png.read_bytes()[16:24]
            self.assertEqual(struct.unpack(">II", header),
                             (nav.PORTRAIT_WIDTH, nav.PORTRAIT_HEIGHT))

    def test_rejects_short_frame_readout(self):
        self.landscape = b"\xff" * 16
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AssertionError):
                self.runner.capture(self.qmp, "x", Path(tmp) / "r.json", "nav")

    def test_rejects_missing_property_readout(self):
        def broken(command, arguments=None):
            return {"error": {"class": "GenericError"}}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AssertionError):
                self.runner.capture(broken, "x", Path(tmp) / "r.json", "nav")

    def test_injection_uses_raw_gt911_sensor_properties(self):
        calls = []

        def qmp(command, arguments=None):
            calls.append((command, arguments))
            return {"return": None}
        result = self.runner.inject(qmp, (240, 453))
        self.assertTrue(all(item["ok"] for item in result.values()))
        props = [arguments["property"] for _, arguments in calls]
        self.assertEqual(props, ["ybx-touch-x", "ybx-touch-y", "ybx-touch-state"])
        values = [arguments["value"] for _, arguments in calls]
        self.assertEqual(values, [240, 453, 1])


@unittest.skipUnless(os.environ.get("YBX_GUEST_EVIDENCE_DIR"),
                     "set YBX_GUEST_EVIDENCE_DIR to the recorded guest captures")
class GenuineFrameTests(unittest.TestCase):
    """Re-derive the shipped thresholds from the real guest frames."""

    FRAMES = {
        "home": "s3-x4pro-gpio6-pcpu-irq-0042-2s-final-before.png",
        "more-dialog": "s3-x4pro-gpio6-pcpu-irq-0042-2s-final-after.png",
        "ybx-settings": "s3-batch2-0043-browser-home-more-settings.png",
        "system-settings": "s3-batch2-0043-browser-system-settings.png",
    }

    def frame(self, name):
        path = Path(os.environ["YBX_GUEST_EVIDENCE_DIR"]) / self.FRAMES[name]
        width, height, packed = packed_portrait_from_png(path)
        self.assertEqual((width, height), (nav.PORTRAIT_WIDTH, nav.PORTRAIT_HEIGHT))
        self.assertEqual(len(packed), nav.FRAME_BYTES)
        return packed

    def test_genuine_frames_classify(self):
        for name in self.FRAMES:
            self.assertEqual(nav.screen_state(self.frame(name)), name)

    def test_genuine_more_dialog_border_runs(self):
        dialog = self.frame("more-dialog")
        self.assertGreaterEqual(nav.longest_dark_run(dialog, nav.BAND_DIALOG_TOP), 340)
        self.assertGreaterEqual(nav.longest_dark_run(dialog, nav.BAND_DIALOG_BOTTOM), 340)
        for name in ("home", "ybx-settings", "system-settings"):
            self.assertLess(nav.longest_dark_run(self.frame(name),
                                                 nav.BAND_DIALOG_TOP),
                            nav.DIALOG_RUN_MIN)

    def test_genuine_settings_rows_match_their_bands(self):
        self.assertGreaterEqual(nav.dark_count(self.frame("ybx-settings"),
                                               nav.BAND_YBX_SETTINGS_SYSTEM_ROW),
                                nav.YBX_SETTINGS_SYSTEM_ROW_INK_MIN)
        self.assertGreaterEqual(nav.dark_count(self.frame("system-settings"),
                                               nav.BAND_SYSTEM_SETTINGS_CONNECTIVITY),
                                nav.SYSTEM_SETTINGS_CONNECTIVITY_INK_MIN)

    def test_settings_rows_are_absent_elsewhere(self):
        for name in ("home", "more-dialog", "system-settings"):
            self.assertLess(nav.dark_count(self.frame(name),
                                           nav.BAND_YBX_SETTINGS_SYSTEM_ROW),
                            nav.YBX_SETTINGS_SYSTEM_ROW_INK_MIN // 4)

    def test_home_header_band_is_home_only(self):
        for name in ("home", "more-dialog"):
            self.assertGreaterEqual(nav.dark_count(self.frame(name), nav.BAND_HOME_HEADER),
                                    nav.HOME_HEADER_INK_MIN)
        for name in ("ybx-settings", "system-settings"):
            self.assertLess(nav.dark_count(self.frame(name), nav.BAND_HOME_HEADER),
                            nav.HOME_HEADER_INK_MIN)

    def test_genuine_home_and_dialog_frames_keep_their_sha_identity(self):
        # The recorded native run: e79c2ad5... -> 8b81cec4... on the landscape
        # RAM readout. The portrait repack must therefore differ too.
        home, dialog = self.frame("home"), self.frame("more-dialog")
        self.assertNotEqual(hashlib.sha256(home).hexdigest(),
                            hashlib.sha256(dialog).hexdigest())


if __name__ == "__main__":
    unittest.main()
