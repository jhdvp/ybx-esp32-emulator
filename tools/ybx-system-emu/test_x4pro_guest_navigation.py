"""Host tests for multi-step X4 Pro guest navigation acceptance logic.

No QEMU, no network, no shared browser emulator. Frames are either built
byte-by-byte in the test (clearly synthetic) or, when the recorded genuine
guest captures are available, read from them through YBX_GUEST_EVIDENCE_DIR.
The browser channel is exercised against an in-process fake transport that
never opens a socket.
"""
import contextlib
import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

import argparse

from epd_frame_io import save_mono_png
from probe_x4pro_guest_touch_ui import qemu_command, x4pro_raw_to_portrait
import x4pro_guest_navigation as nav
import run_x4pro_guest_navigation as runner

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "tools" / "ybx-system-emu" / "run_x4pro_guest_navigation.py"

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
    """Delegate to the shipped unpacker; kept so genuine-frame tests read clearly."""
    return nav.portrait_from_mono_png(Path(path).read_bytes())


class TargetGeometryTests(unittest.TestCase):
    def test_targets_inside_sensor_bounds(self):
        for name, target in nav.TARGETS.items():
            x, y = target["raw_xy"]
            self.assertTrue(0 <= x <= 479 and 0 <= y <= 799, name)

    def test_measured_gt911_taps_are_the_corrected_coordinates(self):
        # 0062: the 0048 report guessed (240,681) and (66,63); the measured
        # working taps are (210,679) and (35,61).
        self.assertEqual(nav.TARGETS["home-more"]["raw_xy"], (350, 760))
        self.assertEqual(nav.TARGETS["more-settings"]["raw_xy"], (240, 453))
        self.assertEqual(nav.TARGETS["settings-system-settings"]["raw_xy"], (210, 679))
        self.assertEqual(nav.TARGETS["system-settings-back"]["raw_xy"], (35, 61))

    def test_corrected_targets_land_inside_measured_regions(self):
        # Measured from the genuine frames (row text x16..444 y670..693;
        # header arrow x24..122 y52..74). The 0048 guesses were not derived
        # from the regions the guest actually routes.
        x, y = nav.TARGETS["settings-system-settings"]["raw_xy"]
        self.assertTrue(16 <= x < 444 and 670 <= y < 693, (x, y))
        x, y = nav.TARGETS["system-settings-back"]["raw_xy"]
        self.assertTrue(24 <= x < 123 and 52 <= y < 74, (x, y))

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


class AcceptancePlanTests(unittest.TestCase):
    def test_full_chain_is_strictly_ordered_with_measured_points(self):
        plan = nav.ordered_acceptance_plan()
        self.assertEqual([s["index"] for s in plan], [0, 1, 2, 3])
        self.assertEqual([(s["raw_xy"], s["expect_before"], s["expect_after"])
                          for s in plan], [
            ([350, 760], "home", "more-dialog"),
            ([240, 453], "more-dialog", "ybx-settings"),
            ([210, 679], "ybx-settings", "system-settings"),
            ([35, 61], "system-settings", "ybx-settings"),
        ])

    def test_every_step_carries_the_accepted_sha_gate(self):
        plan = nav.ordered_acceptance_plan()
        for step in plan:
            self.assertEqual(step["expected_sha256"],
                             nav.ACCEPTED_SCREEN_SHAS[step["expect_after"]])
            self.assertFalse(step["requires_accepted_sha"])
        self.assertEqual(len(nav.ACCEPTED_SCREEN_SHAS), 4)

    def test_back_step_replays_the_open_settings_frame(self):
        plan = nav.ordered_acceptance_plan()
        self.assertEqual(plan[1]["expected_sha256"], plan[3]["expected_sha256"],
                         "the back step must land on the same ybx-settings frame")
        self.assertEqual(plan[3]["replay_of"], plan[1]["name"])

    def test_require_accepted_shas_flips_the_gate(self):
        plan = nav.ordered_acceptance_plan(require_accepted_shas=True)
        self.assertTrue(all(s["requires_accepted_sha"] for s in plan))

    def test_requiring_shas_for_a_screen_none_has_captured_is_refused(self):
        nav.SEQUENCES.setdefault("synthetic-sha-less", [
            {"name": "into-void", "target": "more-dismiss",
             "expect_before": "more-dialog", "expect_after": "unknown"}])
        self.addCleanup(nav.SEQUENCES.pop, "synthetic-sha-less", None)
        with self.assertRaises(ValueError):
            nav.ordered_acceptance_plan("synthetic-sha-less",
                                        require_accepted_shas=True)

    def test_order_check_accepts_the_expected_chain(self):
        plan = nav.ordered_acceptance_plan()
        observed = [{"step": s["name"], "screen_before": s["expect_before"],
                     "screen_after": s["expect_after"]} for s in plan]
        self.assertTrue(nav.validate_step_order(plan, observed)["ok"])

    def test_order_check_rejects_a_skipped_screen(self):
        plan = nav.ordered_acceptance_plan()
        observed = [{"step": "open-more", "screen_before": "home",
                     "screen_after": "more-dialog"},
                    {"step": "open-settings", "screen_before": "more-dialog",
                     "screen_after": "system-settings"}]
        check = nav.validate_step_order(plan, observed)
        self.assertFalse(check["ok"])
        self.assertEqual(check["reason"], "step_1_reached_system-settings")

    def test_order_check_rejects_a_stale_starting_screen(self):
        plan = nav.ordered_acceptance_plan()
        observed = [{"step": "open-more", "screen_before": "home",
                     "screen_after": "more-dialog"},
                    {"step": "open-settings", "screen_before": "home",
                     "screen_after": "ybx-settings"}]
        check = nav.validate_step_order(plan, observed)
        self.assertFalse(check["ok"])
        self.assertEqual(check["reason"], "step_1_started_on_home")

    def test_order_check_rejects_more_steps_than_planned(self):
        plan = nav.ordered_acceptance_plan("more-and-dismiss")
        observed = [{"step": "a", "screen_before": "home", "screen_after": "more-dialog"},
                    {"step": "b", "screen_before": "more-dialog", "screen_after": "home"},
                    {"step": "c", "screen_before": "home", "screen_after": "home"}]
        self.assertFalse(nav.validate_step_order(plan, observed)["ok"])

    def test_order_check_rejects_empty_inputs(self):
        plan = nav.ordered_acceptance_plan()
        self.assertFalse(nav.validate_step_order(plan, [])["ok"])
        self.assertFalse(nav.validate_step_order([], [{"step": "x"}])["ok"])


class StepGateTests(unittest.TestCase):
    def setUp(self):
        self.step = {"name": "open-settings", "target": "more-settings",
                     "expect_before": "more-dialog", "expect_after": "ybx-settings",
                     "expected_sha256": nav.ACCEPTED_SCREEN_SHAS["ybx-settings"],
                     "requires_accepted_sha": False}
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

    def test_sha_gate_reports_expectation_only_by_default(self):
        self.step["expected_sha256"] = None
        result = self.assess()
        self.assertTrue(result["passed"], result["failure_codes"])
        self.assertEqual(result["accepted_sha_gate"], "expectation_only")
        self.assertFalse(result["requires_accepted_sha"])

    def test_sha_gate_matches_the_accepted_frame(self):
        self.after["frame_sha256"] = nav.ACCEPTED_SCREEN_SHAS["ybx-settings"]
        result = self.assess()
        self.assertTrue(result["passed"], result["failure_codes"])
        self.assertEqual(result["accepted_sha_gate"], "matched")

    def test_sha_gate_mismatch_recorded_without_requiring_it(self):
        self.after["frame_sha256"] = "some-other-real-repaint"
        result = self.assess()
        self.assertTrue(result["passed"], "a legitimate repaint must not fail by default")
        self.assertEqual(result["accepted_sha_gate"], "mismatched")

    def test_required_sha_gate_rejects_a_different_frame(self):
        self.step["requires_accepted_sha"] = True
        self.after["frame_sha256"] = "some-other-real-repaint"
        result = self.assess()
        self.assertFalse(result["passed"])
        self.assertIn("accepted_sha256_mismatch", result["failure_codes"])

    def test_unchanged_panel_fails_even_when_gate_is_relaxed(self):
        self.step["requires_accepted_sha"] = False
        self.step["expected_sha256"] = None
        self.after["frame_sha256"] = self.before["frame_sha256"]
        result = self.assess()
        self.assertFalse(result["passed"])
        self.assertIn("panel_pixels_unchanged", result["failure_codes"])

    def test_not_ready_guest_rejected(self):
        self.before["ready"] = False
        result = self.assess()
        self.assertFalse(result["passed"])
        self.assertIn("guest_not_ready", result["failure_codes"])

    def test_browser_channel_records_consumption_as_unobserved(self):
        result = nav.assess_step(self.step, self.before, self.after, {}, {},
                                 {"api-tap": {"accepted": True}})
        self.assertTrue(result["passed"], result["failure_codes"])
        self.assertFalse(result["guest_gt911_i2c_consumption_observed"])
        self.assertFalse(result["full_acceptance"])
        self.assertEqual(result["gt911_point_reads"], 0)

    def test_native_channel_marks_full_acceptance(self):
        result = self.assess()
        self.assertTrue(result["guest_gt911_i2c_consumption_observed"])
        self.assertTrue(result["full_acceptance"])


class SequenceVerdictTests(unittest.TestCase):
    def step_result(self, name, passed, sha, screen, before="home"):
        return {"step": name, "target": name, "passed": passed,
                "failure_codes": [] if passed else ["panel_pixels_unchanged"],
                "frame_sha256_after": sha, "screen_after": screen,
                "screen_before": before, "expected_screen_after": screen,
                "expected_screen_before": before, "raw_xy": [0, 0]}

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

    def test_verdict_carries_the_strict_order_check(self):
        plan = nav.ordered_acceptance_plan()[:2]
        results = [self.step_result("open-more", True, "a", "more-dialog"),
                   self.step_result("open-settings", True, "b", "ybx-settings",
                                    before="more-dialog")]
        verdict = nav.assess_sequence(results, plan)
        self.assertTrue(verdict["strict_order_ok"])
        self.assertEqual(verdict["order_check"]["steps_checked"], 2)
        results[1]["screen_after"] = "system-settings"
        verdict = nav.assess_sequence(results, plan)
        self.assertFalse(verdict["strict_order_ok"])

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
        first = nav.resolve_sequence(nav.DEFAULT_SEQUENCE)
        first[0]["target"] = "tampered"
        self.assertEqual(nav.SEQUENCES[nav.DEFAULT_SEQUENCE][0]["target"],
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


class FakeBrowser:
    """In-process stand-in for a serve_x4pro_browser.py instance.

    Serves real PNG bytes built from the synthetic frames above; no socket is
    ever opened. `frames` advances exactly when a tap was accepted, which is
    what the panel-did-not-change gate is tested against.
    """

    def __init__(self, screens, ready=True, frozen=False, cleanup=lambda _fn: None):
        self.screens = list(screens)
        self.ready = ready
        self.frozen = frozen
        self.cursor = 0
        self.frames = 1
        self.taps = []
        self._pngs = {}
        self._tmp = tempfile.TemporaryDirectory()
        cleanup(self._tmp.cleanup)
        for index, frame in enumerate(self.screens):
            path = Path(self._tmp.name) / f"screen{index}.png"
            save_mono_png(path, frame, nav.PORTRAIT_WIDTH, nav.PORTRAIT_HEIGHT)
            self._pngs[index] = path.read_bytes()

    # -- transport API -----------------------------------------------------
    def get_json(self, path):
        assert path == "/api/state", path
        if self.cursor >= len(self.screens):
            raise AssertionError("script ran out of frames")
        return {"frames": self.frames, "nonwhite": 1234, "ready": self.ready,
                "sha256": f"landscape-{self.cursor:02d}"}

    def get_bytes(self, path):
        assert path == "/screen.png", path
        return self._pngs[self.cursor]

    def post_json(self, path, payload):
        assert path == "/api/tap", path
        self.taps.append(dict(payload))
        if not self.frozen and self.cursor + 1 < len(self.screens):
            self.cursor += 1
            self.frames += 1
        return 200, {"accepted": True, "frame_not_yet_advanced": True}


def run_browser_with(screens, tmp: Path, *, ready=True, frozen=False,
                     require_accepted_shas=False,
                     sequence=nav.DEFAULT_SEQUENCE, cleanup=lambda _fn: None):
    args = argparse_namespace(base_url="http://192.0.2.1:9/test", out=tmp / "verdict.json",
                              sequence=sequence, allow_loopback=True,
                              require_accepted_shas=require_accepted_shas,
                              settle_seconds=1.0, poll_seconds=0.0,
                              tap_duration_ms=150, timeout_seconds=5.0)
    fake = FakeBrowser(screens, ready=ready, frozen=frozen, cleanup=cleanup)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = runner.run_browser(args, FakeParser(),
                                  transport_factory=lambda url, timeout: fake,
                                  sleep=lambda _s: None)
    report = json.loads((tmp / "verdict.json").read_text()) if (tmp / "verdict.json").exists() else None
    return code, fake, report, buffer.getvalue()


def argparse_namespace(**kwargs):
    return argparse.Namespace(**kwargs)


class FakeParser:
    def error(self, message):
        raise SystemExit(f"parser error: {message}")


class BrowserChannelGateTests(unittest.TestCase):
    """The browser runner's gates, driven entirely in-process."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_full_chain_accepted_step_by_step(self):
        screens = [home_frame(), home_frame(with_dialog=True), ybx_settings_frame(),
                   system_settings_frame(), ybx_settings_frame()]
        code, fake, report, _ = run_browser_with(screens, self.tmp,
                                                 cleanup=self.addCleanup)
        self.assertEqual(code, 0)
        self.assertTrue(report["navigation_sequence_verified"])
        self.assertTrue(report["strict_order_ok"])
        self.assertIsNone(report["stopped_at_step"])
        self.assertEqual(fake.taps, [
            {"x": 350, "y": 760, "duration_ms": 150},
            {"x": 240, "y": 453, "duration_ms": 150},
            {"x": 210, "y": 679, "duration_ms": 150},
            {"x": 35, "y": 61, "duration_ms": 150},
        ])
        self.assertEqual([s["screen_after"] for s in report["steps"]],
                         ["more-dialog", "ybx-settings", "system-settings", "ybx-settings"])
        # The gate outcome is recorded against the accepted frames; the fake
        # server serves synthetic landscape SHAs, so they mismatch while the
        # screen predicates (real pixels) keep the run passing.
        self.assertEqual([s["accepted_sha_gate"] for s in report["steps"]],
                         ["mismatched"] * 4)
        self.assertFalse(report["require_accepted_shas"])
        for step in report["steps"]:
            self.assertTrue(Path(step["png_after"]).exists())
            self.assertNotEqual(step["frame_sha256_before"], step["frame_sha256_after"])
            self.assertTrue(step["png_after"].endswith("after.png"))
        self.assertFalse(report["full_acceptance"])
        self.assertFalse(report["guest_gt911_i2c_consumption_observed"])
        # per-step PNGs are the real served bytes of the reached screen
        expected = Path(self._tmp.name) / "expected-step4.png"
        save_mono_png(expected, ybx_settings_frame(), nav.PORTRAIT_WIDTH, nav.PORTRAIT_HEIGHT)
        self.assertEqual(Path(report["steps"][3]["png_after"]).read_bytes(),
                         expected.read_bytes())

    def test_step_that_does_not_change_the_panel_fails_loudly(self):
        screens = [home_frame(), home_frame(with_dialog=True), ybx_settings_frame(),
                   system_settings_frame(), ybx_settings_frame()]
        code, fake, report, _ = run_browser_with(screens, self.tmp, frozen=True,
                                                 cleanup=self.addCleanup)
        self.assertEqual(code, 1)
        self.assertFalse(report["navigation_sequence_verified"])
        self.assertEqual(report["stopped_at_step"], "open-more")
        self.assertEqual(report["first_failing_step"], "open-more")
        self.assertIn("panel_pixels_unchanged", report["steps"][0]["failure_codes"])
        self.assertIn("epd_refresh_count_not_increased", report["steps"][0]["failure_codes"])
        self.assertEqual(report["steps"][0]["frame_sha256_before"],
                         report["steps"][0]["frame_sha256_after"])
        self.assertEqual(len(fake.taps), 1, "the chain must stop at the failed step")

    def test_wrong_landing_screen_rejects_the_step(self):
        screens = [home_frame(), system_settings_frame(), ybx_settings_frame(),
                   system_settings_frame(), ybx_settings_frame()]
        code, fake, report, _ = run_browser_with(screens, self.tmp, cleanup=self.addCleanup)
        self.assertEqual(code, 1)
        self.assertEqual(report["stopped_at_step"], "open-more")
        self.assertIn("target_screen_is_system-settings", report["steps"][0]["failure_codes"])
        self.assertEqual(report["order_check"]["ok"], False)
        self.assertEqual(report["order_check"]["reason"], "step_0_reached_system-settings")

    def test_not_ready_guest_is_refused_before_any_tap(self):
        screens = [home_frame(), home_frame(with_dialog=True)]
        code, fake, report, _ = run_browser_with(screens, self.tmp, ready=False, cleanup=self.addCleanup)
        self.assertEqual(code, 1)
        self.assertEqual(fake.taps, [], "no tap may be injected into a not-ready guest")
        self.assertEqual(report["steps"][0]["failure_codes"], ["guest_not_ready"])
        self.assertEqual(report["stopped_at_step"], "open-more")

    def test_accepted_sha_requirement_rejects_a_different_repaint(self):
        screens = [home_frame(), home_frame(with_dialog=True), ybx_settings_frame(),
                   system_settings_frame(), ybx_settings_frame()]
        code, _fake, report, _ = run_browser_with(
            screens, self.tmp, require_accepted_shas=True, cleanup=self.addCleanup)
        # The fake server reports synthetic landscape SHAs, so requiring the
        # accepted frames must fail on step one rather than pass silently.
        self.assertEqual(code, 1)
        self.assertEqual(report["stopped_at_step"], "open-more")
        self.assertIn("accepted_sha256_mismatch", report["steps"][0]["failure_codes"])

    def test_unreachable_instance_produces_an_error_verdict(self):
        class Dead:
            def get_json(self, path):
                raise runner.BrowserError("instance unreachable: refused")
            def get_bytes(self, path):
                raise AssertionError("must not be reached")
            def post_json(self, path, payload):
                raise AssertionError("must not be reached")
        args = argparse_namespace(base_url="http://192.0.2.1:9/x", out=self.tmp / "v.json",
                                  sequence=nav.DEFAULT_SEQUENCE, allow_loopback=True,
                                  require_accepted_shas=False, settle_seconds=1.0,
                                  poll_seconds=0.0, tap_duration_ms=150, timeout_seconds=1.0)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = runner.run_browser(args, FakeParser(),
                                      transport_factory=lambda url, timeout: Dead(),
                                      sleep=lambda _s: None)
        self.assertEqual(code, 3)
        report = json.loads((self.tmp / "v.json").read_text())
        self.assertIn("unreachable", report["error"])
        self.assertFalse(report["navigation_sequence_verified"])


class BrowserChannelGuardTests(unittest.TestCase):
    """The runner must never guess an instance and never touch the live one silently."""

    def test_missing_or_malformed_base_url_is_refused(self):
        for bad in (None, "", "   ", "ftp://example", "9"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                runner.validate_base_url(bad)

    def test_loopback_needs_a_deliberate_second_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse_namespace(base_url="http://127.0.0.1:8784",
                                      out=Path(directory) / "v.json",
                                      sequence=nav.DEFAULT_SEQUENCE,
                                      allow_loopback=False, require_accepted_shas=False,
                                      settle_seconds=1.0, poll_seconds=0.0,
                                      tap_duration_ms=150, timeout_seconds=1.0)
            buffer = io.StringIO()
            with contextlib.redirect_stderr(buffer):
                code = runner.run_browser(args, FakeParser(),
                                          transport_factory=lambda url, timeout: FakeBrowser([]),
                                          sleep=lambda _s: None)
            self.assertEqual(code, 2)
            self.assertIn("--allow-loopback", buffer.getvalue())

    def test_cli_without_base_url_asks_for_native_inputs(self):
        result = subprocess.run([sys.executable, str(RUNNER)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn("native channel requires", result.stderr)

    def test_cli_loopback_guard_reaches_the_real_process(self):
        result = subprocess.run([sys.executable, str(RUNNER),
                                 "--base-url", "http://127.0.0.1:8784",
                                 "--out", "/tmp/ybx-nav-guard.json"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--allow-loopback", result.stderr)
        self.assertFalse(Path("/tmp/ybx-nav-guard.json").exists(),
                         "a refused run must not write a verdict")


class PortraitPngRoundTripTests(unittest.TestCase):
    def test_roundtrip_matches_the_writer(self):
        from epd_frame_io import save_mono_png
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.png"
            frame = home_frame(with_dialog=True)
            save_mono_png(path, frame, nav.PORTRAIT_WIDTH, nav.PORTRAIT_HEIGHT)
            width, height, packed = nav.portrait_from_mono_png(path.read_bytes())
            self.assertEqual((width, height), (nav.PORTRAIT_WIDTH, nav.PORTRAIT_HEIGHT))
            self.assertEqual(packed, frame)

    def test_rejects_a_non_png(self):
        with self.assertRaises(ValueError):
            nav.portrait_from_mono_png(b"not a png at all")

    def test_rejects_wrong_bit_depth(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.png"
            frame = home_frame()
            save_mono_png(path, frame, nav.PORTRAIT_WIDTH, nav.PORTRAIT_HEIGHT)
            data = bytearray(path.read_bytes())
            data[24] = 8  # IHDR bit depth byte
            with self.assertRaises(ValueError):
                nav.portrait_from_mono_png(bytes(data))


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

    def test_corrected_taps_land_inside_the_measured_regions(self):
        # A GT911 contact needs no ink under the finger: the firmware routes by
        # row geometry. These assertions check the corrected points fall inside
        # the regions measured from the genuine frames, not onto dark pixels.
        ybx = self.frame("ybx-settings")
        rx0, ry0, rx1, ry1 = nav.ink_bbox(ybx, nav.BAND_YBX_SETTINGS_SYSTEM_ROW)
        x, y = nav.TARGETS["settings-system-settings"]["raw_xy"]
        self.assertTrue(rx0 <= x < rx1 and ry0 <= y < ry1,
                        f"(210,679) outside the measured row bbox {(rx0, ry0, rx1, ry1)}")
        sysf = self.frame("system-settings")
        ax0, ay0, ax1, ay1 = nav.ink_bbox(sysf, (10, 45, 122, 90))
        x, y = nav.TARGETS["system-settings-back"]["raw_xy"]
        self.assertTrue(ax0 <= x < ax1 and ay0 <= y < ay1,
                        f"(35,61) outside the measured header arrow bbox "
                        f"{(ax0, ay0, ax1, ay1)}")

    def test_genuine_back_frame_replays_the_settings_pixels(self):
        path = (Path(os.environ["YBX_GUEST_EVIDENCE_DIR"])
                / "s3-batch2-0043-browser-back-from-system-settings.png")
        _, _, back = packed_portrait_from_png(path)
        self.assertEqual(back, self.frame("ybx-settings"))
        self.assertEqual(nav.screen_state(back), "ybx-settings")

    def test_measured_region_bboxes_are_stable(self):
        ybx = self.frame("ybx-settings")
        self.assertEqual(nav.ink_bbox(ybx, nav.BAND_YBX_SETTINGS_SYSTEM_ROW),
                         (16, 670, 444, 693))
        sysf = self.frame("system-settings")
        self.assertEqual(nav.ink_bbox(sysf, (10, 45, 122, 90)), (24, 52, 122, 74))

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
