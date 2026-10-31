"""Host tests for the measured CrossPoint navigation plan.

No sockets, no Docker, no guest: the runner is exercised through an in-process
bridge that serves real PNG/state pairs generated from synthetic 1bpp frames.
The production plan's measured SHAs are tested as data; the runner's gates are
tested against synthetic frames so the test can deliberately break a step.
"""
import contextlib
import hashlib
import io
import tempfile
import unittest
from pathlib import Path

import crosspoint_navigation as cp
from epd_frame_io import save_mono_png


def _portrait(seed: int) -> bytes:
    buffer = bytearray(b"\xff" * cp.PANEL_BYTES)
    for index in range(96):
        x = (seed * 137 + index * 37) % cp.PANEL_WIDTH
        y = (seed * 89 + index * 53) % cp.PANEL_HEIGHT
        buffer[y * (cp.PANEL_WIDTH // 8) + x // 8] &= ~(0x80 >> (x % 8))
    return bytes(buffer)


class FakeBridge:
    """In-process stand-in for BrowserBridge; advances one scripted frame per tap."""

    def __init__(self, portraits, *, advance_on_tap=None, ready=True,
                 firmware_sha=cp.MEASURED_FIRMWARE_SHA256, png_override=None):
        self.portraits = list(portraits)
        self.shas = [hashlib.sha256(
            cp.portrait_to_landscape(p, cp.PANEL_WIDTH, cp.PANEL_HEIGHT)).hexdigest()
            for p in self.portraits]
        self.position = 0
        self.counter = 2
        self.advance_on_tap = list(advance_on_tap or [])
        self.ready = ready
        self.firmware_sha = firmware_sha
        self.png_override = png_override
        self.taps = []
        self.reboots = 0
        self._png_cache = {}
        self._temp = tempfile.TemporaryDirectory()

    def close(self):
        self._temp.cleanup()

    def _png(self, position):
        if position not in self._png_cache:
            path = Path(self._temp.name) / f"frame-{position}.png"
            save_mono_png(path, self.portraits[position], cp.PANEL_WIDTH, cp.PANEL_HEIGHT)
            self._png_cache[position] = path.read_bytes()
        return self._png_cache[position]

    def state(self):
        return {
            "frames": self.counter,
            "sha256": self.shas[self.position],
            "ready": self.ready,
            "firmware": {"sha256": self.firmware_sha},
        }

    def screen_png(self):
        if self.png_override is not None:
            return self.png_override
        return self._png(self.position)

    def tap(self, x, y, duration_ms=150):
        self.taps.append((x, y, duration_ms))
        self.counter += 1
        if self.advance_on_tap:
            self.position = self.advance_on_tap.pop(0)
        return {"accepted": True}

    def reboot(self):
        self.reboots += 1
        return {"accepted": True}


def synthetic_plan(bridge, count=3):
    """Build a `count`-step plan over the bridge's frames in order."""
    steps = []
    targets = ["home-browse-files", "header-back", "home-library", "home-settings"]
    for index in range(count):
        steps.append({
            "name": f"step-{index}",
            "target": targets[index % len(targets)],
            "expect_screen_before": cp.screen_name(bridge.shas[index]),
            "expect_screen_after": cp.screen_name(bridge.shas[index + 1]),
            "expect_frame_sha256_before": bridge.shas[index],
            "expect_frame_sha256_after": bridge.shas[index + 1],
            "observed_transients": [],
            "index": index,
            "raw_xy": [10 + index, 20 + index],
        })
    return steps


def run(bridge, plan, **kwargs):
    options = {"sequence": "synthetic", "step_timeout": 0.05,
               "settle_seconds": 0.05, "settle_polls": 1,
               "poll_interval": 0.0, "out_dir": Path(tempfile.mkdtemp())}
    options.update(kwargs)
    return cp.run_plan(plan, bridge, **options)


class PlanDataTests(unittest.TestCase):
    def test_home_tour_is_the_measured_chain(self):
        plan = cp.build_plan("home-tour")
        self.assertEqual([step["raw_xy"] for step in plan],
                         [[240, 346], [36, 60], [240, 418], [36, 60],
                          [240, 562], [36, 60]])
        self.assertEqual([step["expect_frame_sha256_after"] for step in plan], [
            cp.BROWSE_FILES, cp.HOME_BROWSE_SELECTED, cp.LIBRARY,
            cp.HOME_LIBRARY_SELECTED, cp.SETTINGS, cp.HOME_SETTINGS_SELECTED,
        ])
        self.assertEqual(cp.initial_frame_sha256("home-tour"),
                         cp.HOME_BROWSE_SELECTED)

    def test_plan_chains_frames_and_screen_names(self):
        for name in cp.SEQUENCES:
            plan = cp.build_plan(name)
            for previous, step in zip(plan, plan[1:]):
                self.assertEqual(step["expect_frame_sha256_before"],
                                 previous["expect_frame_sha256_after"])
                self.assertEqual(step["expect_screen_before"],
                                 previous["expect_screen_after"])

    def test_measured_frames_are_named_and_verified_shape(self):
        for sha, name in cp.SCREEN_NAMES.items():
            self.assertEqual(len(sha), 64)
            self.assertEqual(cp.screen_name(sha), name)
            int(sha, 16)
        self.assertEqual(cp.screen_name("0" * 64), "unknown")
        self.assertEqual(cp.screen_name(None), "unknown")

    def test_library_and_settings_transients_are_recorded(self):
        plan = cp.build_plan("home-tour")
        self.assertIn(cp.HOME_LIBRARY_SELECTED, plan[2]["observed_transients"])
        self.assertIn(cp.LIBRARY_INDEX_POPUP, plan[2]["observed_transients"])
        self.assertEqual(plan[4]["observed_transients"],
                         [cp.HOME_SETTINGS_SELECTED])

    def test_unknown_sequence_is_refused(self):
        with self.assertRaises(ValueError):
            cp.build_plan("no-such-sequence")


class PngReadoutTests(unittest.TestCase):
    def test_landscape_inversion_round_trips(self):
        portrait = _portrait(3)
        landscape = cp.portrait_to_landscape(portrait, cp.PANEL_WIDTH, cp.PANEL_HEIGHT)
        self.assertEqual(
            cp.ssd1677_landscape_to_portrait(landscape, cp.PANEL_HEIGHT, cp.PANEL_WIDTH),
            portrait)

    def test_served_frame_sha_matches_state_sha(self):
        bridge = FakeBridge([_portrait(1)])
        self.addCleanup(bridge.close)
        self.assertEqual(cp.served_frame_sha256(bridge.screen_png()),
                         bridge.shas[0])

    def test_wrong_size_png_is_refused(self):
        with self.assertRaises(ValueError):
            cp.served_frame_sha256(b"not a png")


class GateTests(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge([_portrait(index) for index in range(4)])
        self.addCleanup(self.bridge.close)
        self.plan = synthetic_plan(self.bridge)

    def _state(self, index, ready=True):
        return {"frames": index + 2, "sha256": self.bridge.shas[index],
                "ready": ready}

    def test_accept_when_frame_changes_to_target(self):
        result = cp.assess_step(self.plan[0], self._state(0), self._state(1))
        self.assertTrue(result["passed"])
        self.assertTrue(result["frame_changed"])
        self.assertEqual(result["failure_codes"], [])
        self.assertEqual(result["screen_before"], cp.screen_name(self.bridge.shas[0]))
        self.assertEqual(result["screen_after"], cp.screen_name(self.bridge.shas[1]))

    def test_unchanged_frame_fails_loudly(self):
        result = cp.assess_step(self.plan[0], self._state(0), self._state(0))
        self.assertFalse(result["passed"])
        self.assertIn("panel_frame_unchanged", result["failure_codes"])
        self.assertFalse(result["frame_changed"])

    def test_not_ready_guest_fails(self):
        result = cp.assess_step(self.plan[0], self._state(0, ready=False),
                                self._state(1))
        self.assertIn("guest_not_ready", result["failure_codes"])

    def test_wrong_starting_frame_fails(self):
        result = cp.assess_step(self.plan[0], self._state(2), self._state(1))
        self.assertIn("precondition_frame_sha256_mismatch", result["failure_codes"])

    def test_changed_frame_that_is_not_the_target_fails(self):
        result = cp.assess_step(self.plan[0], self._state(0), self._state(2))
        self.assertIn("target_frame_sha256_mismatch", result["failure_codes"])
        self.assertNotIn("panel_frame_unchanged", result["failure_codes"])

    def test_missing_readouts_fail(self):
        result = cp.assess_step(self.plan[0], {"ready": True},
                                {"ready": True})
        self.assertIn("missing_frame_sha256_before", result["failure_codes"])
        self.assertIn("missing_frame_sha256_after", result["failure_codes"])


class OrderTests(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge([_portrait(index) for index in range(4)])
        self.addCleanup(self.bridge.close)
        self.plan = synthetic_plan(self.bridge)
        self.observed = [
            {"step": step["name"],
             "frame_sha256_before": step["expect_frame_sha256_before"],
             "frame_sha256_after": step["expect_frame_sha256_after"]}
            for step in self.plan
        ]

    def test_complete_chain_passes(self):
        check = cp.validate_sequence(self.plan, self.observed)
        self.assertTrue(check["ok"])
        self.assertEqual(check["steps_checked"], 3)

    def test_reordered_steps_are_rejected(self):
        check = cp.validate_sequence(self.plan, [self.observed[1], self.observed[0]])
        self.assertFalse(check["ok"])
        self.assertIn("step_0_is_step-1_not_step-0", check["reason"])

    def test_skipped_step_is_rejected(self):
        check = cp.validate_sequence(self.plan, [self.observed[0], self.observed[2]])
        self.assertFalse(check["ok"])
        self.assertIn("step_1_is_step-2_not_step-1", check["reason"])

    def test_extra_steps_are_rejected(self):
        check = cp.validate_sequence(self.plan, self.observed + [self.observed[0]])
        self.assertEqual(check["reason"], "more_steps_than_planned")

    def test_incomplete_chain_is_rejected(self):
        check = cp.validate_sequence(self.plan, self.observed[:2])
        self.assertEqual(check["reason"], "sequence_incomplete_after_2_of_3_steps")

    def test_unplanned_landing_frame_is_rejected(self):
        tampered = [dict(item) for item in self.observed]
        tampered[1]["frame_sha256_after"] = self.bridge.shas[3]
        check = cp.validate_sequence(self.plan, tampered)
        self.assertEqual(check["reason"], "step_1_reached_unplanned_frame")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge([_portrait(index) for index in range(6)])
        self.addCleanup(self.bridge.close)
        self.plan = synthetic_plan(self.bridge)

    def test_full_chain_accepted_with_one_tap_per_step(self):
        self.bridge.advance_on_tap = [1, 2, 3]
        report = run(self.bridge, self.plan)
        verdict = report["verdict"]
        self.assertEqual(verdict["steps_passed"], 3)
        self.assertEqual(verdict["taps_injected"], 3)
        self.assertTrue(verdict["navigation_sequence_verified"])
        self.assertTrue(verdict["strict_order_ok"])
        self.assertEqual(verdict["frame_sha256_chain"],
                         [self.bridge.shas[0], self.bridge.shas[1],
                          self.bridge.shas[2], self.bridge.shas[3]])
        self.assertEqual([(x, y) for x, y, _ in self.bridge.taps],
                         [(10, 20), (11, 21), (12, 22)])
        self.assertTrue(all(step["png_matches_state"] for step in report["steps"]))
        self.assertFalse(verdict["full_acceptance"])
        self.assertFalse(verdict["guest_gt911_i2c_consumption_observed"])

    def test_unchanged_tap_fails_at_the_first_step_and_stops(self):
        self.bridge.advance_on_tap = [0, 1, 2]
        report = run(self.bridge, self.plan)
        verdict = report["verdict"]
        self.assertFalse(verdict["navigation_sequence_verified"])
        self.assertEqual(verdict["first_failing_step"], "step-0")
        self.assertIn("panel_frame_unchanged",
                      report["steps"][0]["failure_codes"])
        self.assertEqual(len(self.bridge.taps), 1)

    def test_not_ready_guest_is_refused_before_any_tap(self):
        self.bridge.ready = False
        report = run(self.bridge, self.plan)
        self.assertEqual(report["verdict"]["aborted"], "guest_not_ready")
        self.assertEqual(self.bridge.taps, [])
        self.assertEqual(report["verdict"]["navigation_sequence_verified"], False)

    def test_wrong_initial_frame_is_refused_before_any_tap(self):
        self.bridge.position = 2
        report = run(self.bridge, self.plan)
        self.assertEqual(report["verdict"]["aborted"],
                         "precondition_frame_sha256_mismatch")
        self.assertEqual(self.bridge.taps, [])

    def test_wrong_landing_frame_fails_and_order_check_rejects_it(self):
        self.bridge.advance_on_tap = [2, 2, 3]
        report = run(self.bridge, self.plan)
        verdict = report["verdict"]
        self.assertEqual(verdict["first_failing_step"], "step-0")
        self.assertIn("target_frame_sha256_mismatch",
                      report["steps"][0]["failure_codes"])
        self.assertFalse(verdict["strict_order_ok"])
        self.assertIn("step_0_reached_unplanned_frame",
                      verdict["order_check"]["reason"])
        self.assertEqual(len(self.bridge.taps), 1)

    def test_allow_sha_drift_records_but_does_not_hide_an_unchanged_frame(self):
        single = synthetic_plan(self.bridge, count=1)
        self.bridge.advance_on_tap = [2]
        report = run(self.bridge, single, allow_sha_drift=True)
        self.assertEqual(report["verdict"]["steps_passed"], 1)
        self.assertEqual(report["steps"][0]["target_sha256_gate"], "drifted")
        other = FakeBridge([_portrait(index) for index in range(2)])
        self.addCleanup(other.close)
        other.advance_on_tap = [0]
        stuck = run(other, synthetic_plan(other, count=1), allow_sha_drift=True)
        self.assertFalse(stuck["verdict"]["navigation_sequence_verified"])
        self.assertIn("panel_frame_unchanged",
                      stuck["steps"][0]["failure_codes"])

    def test_firmware_gate_refuses_other_images_before_any_tap(self):
        self.bridge.firmware_sha = "f" * 64
        report = run(self.bridge, self.plan)
        self.assertEqual(report["verdict"]["aborted"], "firmware_sha256_mismatch")
        self.assertEqual(self.bridge.taps, [])
        self.assertEqual(report["verdict"]["firmware"]["gate"], "mismatched")
        allowed = run(self.bridge, self.plan, allow_firmware_drift=True,
                      sequence="synthetic")
        self.assertNotEqual(allowed["verdict"]["aborted"], "firmware_sha256_mismatch")

    def test_served_png_that_disagrees_with_state_fails(self):
        other = FakeBridge([_portrait(99)])
        self.addCleanup(other.close)
        self.bridge.png_override = other.screen_png()
        self.bridge.advance_on_tap = [1, 2, 3]
        report = run(self.bridge, self.plan)
        self.assertEqual(report["verdict"]["first_failing_step"], "step-0")
        self.assertIn("evidence_png_sha256_mismatch",
                      report["steps"][0]["failure_codes"])
        self.assertFalse(report["verdict"]["navigation_sequence_verified"])

    def test_reboot_first_waits_for_the_planned_initial_frame(self):
        self.bridge.advance_on_tap = [1, 2, 3]
        report = run(self.bridge, self.plan, reboot_first=True)
        self.assertEqual(self.bridge.reboots, 1)
        self.assertTrue(report["verdict"]["navigation_sequence_verified"])


class CliGuardTests(unittest.TestCase):
    def _main_silently(self, argv):
        with contextlib.redirect_stderr(io.StringIO()):
            return cp.main(argv)

    def test_base_url_is_required(self):
        with self.assertRaises(SystemExit) as caught:
            self._main_silently(["--out", "/tmp/crosspoint-nav-test.json"])
        self.assertEqual(caught.exception.code, 2)

    def test_loopback_needs_an_explicit_allow(self):
        with self.assertRaises(SystemExit) as caught:
            self._main_silently(["--base-url", "http://127.0.0.1:8794",
                                 "--out", "/tmp/crosspoint-nav-test.json"])
        self.assertEqual(caught.exception.code, 2)

    def test_non_http_url_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            self._main_silently(["--base-url", "ftp://127.0.0.1/x",
                                 "--allow-loopback",
                                 "--out", "/tmp/crosspoint-nav-test.json"])
        self.assertEqual(caught.exception.code, 2)

    def test_unknown_sequence_is_refused_by_argparse(self):
        with self.assertRaises(SystemExit) as caught:
            self._main_silently(["--base-url", "http://192.0.2.1:1",
                                 "--sequence", "nope",
                                 "--out", "/tmp/crosspoint-nav-test.json"])
        self.assertEqual(caught.exception.code, 2)

    def test_url_validation_and_loopback_detection(self):
        self.assertEqual(cp.validate_base_url("http://192.0.2.1:8794/"),
                         "http://192.0.2.1:8794")
        for bad in (None, "", "192.0.2.1:8794", "file:///tmp/x"):
            with self.assertRaises(ValueError):
                cp.validate_base_url(bad)
        self.assertTrue(cp.is_loopback_url("http://localhost:8794"))
        self.assertTrue(cp.is_loopback_url("http://127.0.0.1:8794"))
        self.assertFalse(cp.is_loopback_url("http://192.168.10.239:8784"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
