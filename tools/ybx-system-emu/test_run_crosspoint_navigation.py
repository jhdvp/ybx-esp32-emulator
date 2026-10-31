"""Host tests for run_crosspoint_navigation.py.

The runner is exercised against a real in-process HTTP server
(http.server on 127.0.0.1, stdlib only) that emulates the browser bridge
contract (/api/state, /screen.png, /api/tap, /api/reboot, /api/firmware).
No Docker, no QEMU, no guest: the fake bridge scripts the panel, including
broken behaviour the gates must refuse (unchanged frames, not-ready guests,
out-of-order landings, missing readouts, PNG/state disagreements, wrong
firmware). The planned frames use synthetic SSD1677 RAM whose real SHA-256
values are chained into a patched test sequence, so every SHA that flows
through the runner was genuinely computed, never pasted.
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import io
import json
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_crosspoint_navigation as rcn
from epd_frame_io import save_mono_png, ssd1677_landscape_to_portrait

LANDSCAPE_BYTES = 800 * 480 // 8
OTHER_FIRMWARE_SHA256 = "0" * 64


def make_frame(name: str, seed: int) -> dict:
    """Deterministic synthetic landscape RAM + its real SHA + served portrait PNG."""
    landscape = bytearray(b"\xff" * LANDSCAPE_BYTES)
    ink = seed * 977 % (LANDSCAPE_BYTES - 1)
    landscape[ink] = 0x00
    landscape[ink + 1] = 0x00
    landscape = bytes(landscape)
    portrait = ssd1677_landscape_to_portrait(landscape)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "frame.png"
        save_mono_png(path, portrait, rcn.PANEL_WIDTH, rcn.PANEL_HEIGHT)
        png = path.read_bytes()
    return {"name": name, "landscape": landscape,
            "sha": hashlib.sha256(landscape).hexdigest(), "png": png}


class FakeBridge:
    """Scripted panel behind a real HTTP socket; mimics serve_x4pro_browser.py."""

    def __init__(self, frames: dict[str, dict], script: list[dict], *, initial: str,
                 firmware_sha: str = rcn.MEASURED_FIRMWARE_SHA256,
                 firmware_name: str = "s3-flash-complete-rom-test.bin",
                 serve_png_of: str | None = None,
                 blank_sha_after_taps: int | None = None):
        self.frames = frames
        self.script_template = script
        self.initial = initial
        self.firmware_sha = firmware_sha
        self.firmware_name = firmware_name
        self.serve_png_of = serve_png_of          # PNG/state divergence injection
        self.blank_sha_after_taps = blank_sha_after_taps
        self.taps = 0
        self.reboots = 0
        self.firmware_switches: list = []
        self.reset()

    def reset(self):
        """Back to the scripted post-boot Home state (counters are kept)."""
        self.current = self.frames[self.initial]
        self.script = [dict(item) for item in self.script_template]
        self.frames_count = 2                      # guest painted Home: ready
        self.ready = True

    def state(self) -> dict:
        sha = None if (self.blank_sha_after_taps is not None
                       and self.taps > self.blank_sha_after_taps) \
            else self.current["sha"]
        return {"frames": self.frames_count, "nonwhite": 2, "ready": self.ready,
                "sha256": sha,
                "firmware": {"name": self.firmware_name, "sha256": self.firmware_sha}}

    def png(self) -> bytes:
        name = self.serve_png_of or self.current["name"]
        return self.frames[name]["png"]

    def tap(self, x: int, y: int) -> dict:
        self.taps += 1
        if self.script:
            move = self.script.pop(0)
            if tuple(move.get("xy", (x, y))) != (x, y):
                return {"accepted": True}          # unplanned point: guest ignores
            if move.get("to") and self.blank_sha_after_taps is None:
                self.current = self.frames[move["to"]]
            self.frames_count += int(move.get("advance", 2))
        return {"accepted": True, "x": x, "y": y}


class _Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, bridge: FakeBridge, firmware_target: str, **kwargs):
        self.bridge = bridge
        self.firmware_target = firmware_target
        super().__init__(*args, **kwargs)

    def log_message(self, *args):
        pass

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            self._json(200, self.bridge.state())
        elif self.path.startswith("/screen.png"):
            body = self.bridge.png()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(size) if size else b"{}"
        try:
            payload = json.loads(raw)
        except ValueError:
            self._json(400, {"error": "bad json"})
            return
        if self.path == "/api/tap":
            self._json(200, self.bridge.tap(int(payload["x"]), int(payload["y"])))
        elif self.path == "/api/reboot":
            self.bridge.reboots += 1
            self.bridge.reset()
            self._json(200, {"accepted": True, "action": "QEMU system_reset"})
        elif self.path == "/api/firmware":
            name = payload.get("name")
            self.bridge.firmware_switches.append(name)
            if name == self.firmware_target:
                self.bridge.firmware_sha = rcn.MEASURED_FIRMWARE_SHA256
                self.bridge.firmware_name = name
                self.bridge.reset()
                self._json(200, {"accepted": True, "action": "firmware-switch",
                                 "sha256": self.bridge.firmware_sha})
            else:
                self._json(404, {"error": f"unknown firmware {name!r}"})
        else:
            self._json(404, {"error": "not found"})


class BridgeFixture(unittest.TestCase):
    """Shared scaffolding: one fake bridge + real loopback HTTP per test."""

    FRAMES: dict[str, dict] = {}

    @classmethod
    def setUpClass(cls):
        cls.FRAMES = {f"s{i}": make_frame(f"s{i}", i) for i in range(4)}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "verdict.json"

    def serve(self, bridge: FakeBridge) -> str:
        handler = functools.partial(_Handler, bridge=bridge,
                                    firmware_target=rcn.MEASURED_FIRMWARE_NAME)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # LIFO: join must run *after* shutdown/server_close, so register it first.
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def two_step_plan(self) -> list[dict]:
        home, first, second = self.FRAMES["s0"], self.FRAMES["s1"], self.FRAMES["s2"]
        return [
            rcn._step("open-thing", "home-browse-files", "a", "b",
                      home["sha"], first["sha"]),
            rcn._step("back-thing", "header-back", "b", "c",
                      first["sha"], second["sha"]),
        ]

    def cooperative_bridge(self) -> FakeBridge:
        return FakeBridge(self.FRAMES,
                          [{"xy": (240, 346), "to": "s1", "advance": 2},
                           {"xy": (36, 60), "to": "s2", "advance": 2}],
                          initial="s0")

    def run_main(self, base_url: str, extra: list[str] | None = None) -> tuple[int, dict]:
        argv = ["--base-url", base_url, "--allow-loopback", "--sequence", "test-tour",
                "--out", str(self.out), "--step-timeout", "1.5",
                "--settle-seconds", "0.3", "--settle-polls", "2",
                "--poll-interval", "0.01", *(extra or [])]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = rcn.main(argv)
        report = json.loads(self.out.read_text()) if self.out.exists() else {}
        return code, report


class RunnerGateTests(BridgeFixture):
    def test_verified_tour_passes_every_gate(self):
        bridge, plan = self.cooperative_bridge(), self.two_step_plan()
        url = self.serve(bridge)
        with patch.dict(rcn.SEQUENCES, {"test-tour": plan}):
            code, report = self.run_main(url, ["--reboot-first"])
        verdict = report["verdict"]
        self.assertEqual(code, 0)
        self.assertTrue(verdict["navigation_sequence_verified"])
        self.assertTrue(verdict["strict_order_ok"])
        self.assertEqual(verdict["steps_passed"], 2)
        self.assertEqual(verdict["steps_planned"], 2)
        self.assertEqual(verdict["taps_injected"], 2)
        self.assertEqual(bridge.reboots, 1)
        self.assertIsNone(verdict["aborted"])
        self.assertEqual(verdict["firmware"]["gate"], "matched")
        chain = verdict["frame_sha256_chain"]
        self.assertEqual(chain, [self.FRAMES[k]["sha"] for k in ("s0", "s1", "s2")])
        self.assertEqual(len(report["steps"]), 2)
        for step in report["steps"]:
            self.assertTrue(step["passed"])
            self.assertTrue(step["png_matches_state"])
            self.assertEqual(step["target_sha256_gate"], "matched")
            self.assertTrue(Path(step["png"]).exists())

    def test_unchanged_frame_fails_the_gate(self):
        # the guest ignores the tap: frames never advance, RAM stays identical
        bridge = FakeBridge(self.FRAMES,
                            [{"xy": (240, 346), "to": None, "advance": 0},
                             {"xy": (36, 60), "to": "s2", "advance": 2}],
                            initial="s0")
        plan = self.two_step_plan()
        url = self.serve(bridge)
        with patch.dict(rcn.SEQUENCES, {"test-tour": plan}):
            code, report = self.run_main(url)
        verdict = report["verdict"]
        self.assertEqual(code, 1)
        self.assertFalse(verdict["navigation_sequence_verified"])
        self.assertEqual(verdict["steps_passed"], 0)
        self.assertEqual(verdict["taps_injected"], 1)
        codes = report["steps"][0]["failure_codes"]
        self.assertIn("panel_frame_unchanged", codes)
        self.assertIn("frames_not_advanced", codes)
        self.assertIn("target_frame_not_reached_within_timeout", codes)

    def test_not_ready_guest_sends_zero_taps(self):
        bridge = self.cooperative_bridge()
        bridge.ready = False
        bridge.frames_count = 1
        plan = self.two_step_plan()
        url = self.serve(bridge)
        with patch.dict(rcn.SEQUENCES, {"test-tour": plan}):
            code, report = self.run_main(url)
        self.assertEqual(code, 1)
        self.assertEqual(report["verdict"]["aborted"], "guest_not_ready")
        self.assertEqual(report["verdict"]["taps_injected"], 0)
        self.assertEqual(bridge.taps, 0)
        self.assertEqual(report["steps"], [])

    def test_wrong_starting_frame_refuses_with_zero_taps(self):
        bridge = self.cooperative_bridge()
        bridge.current = self.FRAMES["s2"]           # guest parked elsewhere
        plan = self.two_step_plan()
        url = self.serve(bridge)
        with patch.dict(rcn.SEQUENCES, {"test-tour": plan}):
            code, report = self.run_main(url)
        self.assertEqual(code, 1)
        self.assertEqual(report["verdict"]["aborted"], "precondition_frame_sha256_mismatch")
        self.assertEqual(bridge.taps, 0)

    def test_out_of_order_landing_fails_and_breaks_strict_order(self):
        # tap 1 lands on the frame planned for tap 2: the chain is out of order
        bridge = FakeBridge(self.FRAMES,
                            [{"xy": (240, 346), "to": "s2", "advance": 2}],
                            initial="s0")
        plan = self.two_step_plan()
        url = self.serve(bridge)
        with patch.dict(rcn.SEQUENCES, {"test-tour": plan}):
            code, report = self.run_main(url)
        verdict = report["verdict"]
        self.assertEqual(code, 1)
        self.assertFalse(verdict["strict_order_ok"])
        self.assertIn("target_frame_sha256_mismatch",
                      report["steps"][0]["failure_codes"])
        self.assertEqual(verdict["order_check"]["reason"],
                         "step_0_reached_unplanned_frame")

    def test_missing_frame_readout_fails_the_step(self):
        # after the first tap the bridge reports no frame hash at all
        bridge = self.cooperative_bridge()
        bridge.blank_sha_after_taps = 0
        plan = self.two_step_plan()
        url = self.serve(bridge)
        with patch.dict(rcn.SEQUENCES, {"test-tour": plan}):
            code, report = self.run_main(url)
        self.assertEqual(code, 1)
        codes = report["steps"][0]["failure_codes"]
        self.assertIn("missing_frame_sha256_after", codes)
        self.assertIsNone(report["steps"][0]["frame_sha256_after"])

    def test_firmware_mismatch_aborts_before_any_tap(self):
        bridge = self.cooperative_bridge()
        bridge.firmware_sha = OTHER_FIRMWARE_SHA256
        plan = self.two_step_plan()
        url = self.serve(bridge)
        with patch.dict(rcn.SEQUENCES, {"test-tour": plan}):
            code, report = self.run_main(url)
        self.assertEqual(code, 1)
        self.assertEqual(report["verdict"]["aborted"], "firmware_sha256_mismatch")
        self.assertEqual(bridge.taps, 0)
        self.assertEqual(report["verdict"]["firmware"]["gate"], "mismatched")

    def test_allow_firmware_drift_runs_and_records_the_mismatch(self):
        bridge = self.cooperative_bridge()
        bridge.firmware_sha = OTHER_FIRMWARE_SHA256
        plan = self.two_step_plan()
        url = self.serve(bridge)
        with patch.dict(rcn.SEQUENCES, {"test-tour": plan}):
            code, report = self.run_main(url, ["--allow-firmware-drift"])
        self.assertEqual(code, 0)
        self.assertEqual(report["verdict"]["firmware"]["gate"], "mismatched")
        self.assertTrue(report["verdict"]["navigation_sequence_verified"])

    def test_select_firmware_switches_then_runs(self):
        bridge = self.cooperative_bridge()
        bridge.firmware_sha = OTHER_FIRMWARE_SHA256
        plan = self.two_step_plan()
        url = self.serve(bridge)
        with patch.dict(rcn.SEQUENCES, {"test-tour": plan}):
            code, report = self.run_main(
                url, ["--select-firmware", rcn.MEASURED_FIRMWARE_NAME])
        self.assertEqual(code, 0)
        self.assertEqual(bridge.firmware_switches, [rcn.MEASURED_FIRMWARE_NAME])
        self.assertEqual(report["verdict"]["firmware"]["gate"], "matched")

    def test_served_png_disagreeing_with_state_fails(self):
        bridge = self.cooperative_bridge()
        bridge.serve_png_of = "s3"                     # wrong pixels behind the state
        plan = self.two_step_plan()
        url = self.serve(bridge)
        with patch.dict(rcn.SEQUENCES, {"test-tour": plan}):
            code, report = self.run_main(url)
        self.assertEqual(code, 1)
        self.assertIn("evidence_png_sha256_mismatch",
                      report["steps"][0]["failure_codes"])
        self.assertFalse(report["steps"][0]["png_matches_state"])
        self.assertEqual(report["verdict"]["taps_injected"], 1)

    def test_tap_at_unplanned_point_leaves_the_frame_unchanged_and_fails(self):
        # the fake guest only reacts to the planned coordinates: a tap aimed
        # at another row must fail on the changed-frame gate, not pass silently
        plan = self.two_step_plan()
        plan[0]["target"] = "home-library"             # taps (240, 418) instead
        bridge = self.cooperative_bridge()             # script reacts to (240, 346)
        url = self.serve(bridge)
        with patch.dict(rcn.SEQUENCES, {"test-tour": plan}):
            code, report = self.run_main(url)
        self.assertEqual(code, 1)
        codes = report["steps"][0]["failure_codes"]
        self.assertIn("panel_frame_unchanged", codes)
        self.assertEqual(bridge.taps, 1)

    def test_unreachable_instance_reports_bridge_unreachable(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = rcn.main(["--base-url", f"http://127.0.0.1:{port}",
                             "--allow-loopback", "--out", str(self.out),
                             "--timeout-seconds", "1"])
        self.assertEqual(code, 3)
        report = json.loads(self.out.read_text())
        self.assertEqual(report["verdict"]["aborted"], "bridge_unreachable")
        self.assertEqual(report["verdict"]["taps_injected"], 0)


class UnitGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.FRAMES = {f"s{i}": make_frame(f"s{i}", i) for i in range(2)}

    def test_assess_step_wrong_starting_sha_fails(self):
        step = rcn._step("x", "home-browse-files", "a", "b",
                         self.FRAMES["s0"]["sha"], self.FRAMES["s1"]["sha"])
        step = dict(step, index=0, raw_xy=[240, 346])
        before = {"ready": True, "frames": 3, "sha256": self.FRAMES["s1"]["sha"]}
        after = {"ready": True, "frames": 5, "sha256": self.FRAMES["s0"]["sha"]}
        result = rcn.assess_step(step, before, after)
        self.assertFalse(result["passed"])
        self.assertIn("precondition_frame_sha256_mismatch", result["failure_codes"])
        self.assertIn("target_frame_sha256_mismatch", result["failure_codes"])

    def test_assess_step_rejects_missing_readouts(self):
        step = dict(rcn._step("x", "home-browse-files", "a", "b",
                              self.FRAMES["s0"]["sha"], self.FRAMES["s1"]["sha"]),
                    raw_xy=[240, 346])
        before = {"ready": True, "frames": 3, "sha256": None}
        after = {"ready": True, "frames": 5, "sha256": "nothex"}
        result = rcn.assess_step(step, before, after)
        self.assertIn("missing_frame_sha256_before", result["failure_codes"])
        self.assertIn("missing_frame_sha256_after", result["failure_codes"])

    def test_assess_step_not_ready_is_a_hard_failure(self):
        step = dict(rcn._step("x", "home-browse-files", "a", "b",
                              self.FRAMES["s0"]["sha"], self.FRAMES["s1"]["sha"]),
                    raw_xy=[240, 346])
        before = {"ready": False, "frames": 2,
                  "sha256": step["expect_frame_sha256_before"]}
        after = {"ready": True, "frames": 4,
                 "sha256": step["expect_frame_sha256_after"]}
        result = rcn.assess_step(step, before, after)
        self.assertFalse(result["passed"])
        self.assertIn("guest_not_ready", result["failure_codes"])

    def test_validate_sequence_renames_reorders_skips_extras(self):
        sha_a, sha_b = self.FRAMES["s0"]["sha"], self.FRAMES["s1"]["sha"]
        plan = [rcn._step("one", "home-browse-files", "a", "b", sha_a, sha_b),
                rcn._step("two", "header-back", "b", "a", sha_b, sha_a)]
        for index, step in enumerate(plan):
            step["index"] = index
        good = [{"step": s["name"], "frame_sha256_before": s["expect_frame_sha256_before"],
                 "frame_sha256_after": s["expect_frame_sha256_after"]} for s in plan]
        self.assertTrue(rcn.validate_sequence(plan, good)["ok"])
        self.assertEqual(rcn.validate_sequence(plan, [])["reason"], "no_observed_steps")
        self.assertEqual(rcn.validate_sequence([], good)["reason"], "empty_plan")
        self.assertEqual(rcn.validate_sequence(plan, good[:1])["reason"],
                         "sequence_incomplete_after_1_of_2_steps")
        self.assertIn("step_0_is",
                      rcn.validate_sequence(plan, list(reversed(good)))["reason"])
        renamed = [dict(good[0], step="hacked")] + good[1:]
        self.assertIn("step_0_is_hacked_not",
                      rcn.validate_sequence(plan, renamed)["reason"])
        unplanned = [dict(good[0], frame_sha256_after="f" * 64)] + good[1:]
        self.assertEqual(rcn.validate_sequence(plan, unplanned)["reason"],
                         "step_0_reached_unplanned_frame")
        unplanned_start = [dict(good[0], frame_sha256_before="f" * 64)] + good[1:]
        self.assertEqual(rcn.validate_sequence(plan, unplanned_start)["reason"],
                         "step_0_started_on_unplanned_frame")
        self.assertEqual(rcn.validate_sequence(plan, good + [good[0]])["reason"],
                         "more_steps_than_planned")

    def test_build_plan_rejects_broken_plans(self):
        sha_a, sha_b = self.FRAMES["s0"]["sha"], self.FRAMES["s1"]["sha"]
        with self.assertRaises(ValueError):
            rcn.build_plan("no-such-sequence")
        with patch.dict(rcn.SEQUENCES, {"missing-sha": [
                rcn._step("x", "home-browse-files", "a", "b", sha_a, "")]}):
            with self.assertRaises(ValueError):
                rcn.build_plan("missing-sha")
        with patch.dict(rcn.SEQUENCES, {"no-change": [
                rcn._step("x", "home-browse-files", "a", "a", sha_a, sha_a)]}):
            with self.assertRaises(ValueError):
                rcn.build_plan("no-change")
        with patch.dict(rcn.SEQUENCES, {"unchained": [
                rcn._step("x", "home-browse-files", "a", "b", sha_a, sha_b),
                rcn._step("y", "header-back", "b", "a", sha_a, sha_b)]}):
            with self.assertRaises(ValueError):
                rcn.build_plan("unchained")

    def test_measured_home_tour_plan_is_chained_and_gated(self):
        plan = rcn.build_plan("home-tour")
        self.assertEqual(len(plan), 6)
        self.assertEqual(plan[0]["expect_frame_sha256_before"], rcn.HOME_BROWSE_SELECTED)
        self.assertEqual(plan[-1]["expect_frame_sha256_after"], rcn.HOME_SETTINGS_SELECTED)
        for index in range(1, len(plan)):
            self.assertEqual(plan[index]["expect_frame_sha256_before"],
                             plan[index - 1]["expect_frame_sha256_after"])
        self.assertEqual(plan[0]["raw_xy"], [240, 346])
        self.assertEqual(plan[2]["raw_xy"], [240, 418])
        self.assertEqual(plan[4]["raw_xy"], [240, 562])
        self.assertEqual(plan[1]["raw_xy"], [36, 60])
        self.assertEqual(rcn.MEASURED_FIRMWARE_SHA256[:8], "2be70081")

    def test_png_round_trip_recomputes_the_landscape_sha(self):
        frame = self.FRAMES["s1"]
        self.assertEqual(rcn.served_frame_sha256(frame["png"]), frame["sha"])

    def test_allow_sha_drift_never_relaxes_the_changed_frame_gate(self):
        # unit level: drifted target is downgraded by run_plan, but an
        # unchanged panel still carries panel_frame_unchanged from assess_step
        sha_a, sha_b = self.FRAMES["s0"]["sha"], self.FRAMES["s1"]["sha"]
        step = dict(rcn._step("x", "home-browse-files", "a", "b", sha_a, sha_b),
                    index=0, raw_xy=[240, 346])
        before = {"ready": True, "frames": 3, "sha256": sha_a}
        after_same = {"ready": True, "frames": 3, "sha256": sha_a}
        self.assertIn("panel_frame_unchanged",
                      rcn.assess_step(step, before, after_same)["failure_codes"])
        after_other = {"ready": True, "frames": 5, "sha256": "f" * 64}
        self.assertIn("target_frame_sha256_mismatch",
                      rcn.assess_step(step, before, after_other)["failure_codes"])


class CliGuardTests(unittest.TestCase):
    def test_base_url_is_mandatory_and_explicit(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                rcn.main(["--out", "/tmp/whatever.json"])
        self.assertEqual(caught.exception.code, 2)
        for bad in ("", "   ", "ftp://example.invalid/x", "nonsense"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    rcn.validate_base_url(bad)

    def test_loopback_needs_a_deliberate_flag(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = rcn.main(["--base-url", "http://127.0.0.1:9",
                             "--out", "/tmp/whatever.json"])
        self.assertEqual(code, 2)
        self.assertIn("--allow-loopback", stderr.getvalue())

    def test_unknown_sequence_is_a_cli_error(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                rcn.main(["--base-url", "http://example.invalid",
                          "--sequence", "made-up", "--out", "/tmp/x.json"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
