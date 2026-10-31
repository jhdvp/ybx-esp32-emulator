"""Measured tap plan for stock CrossPoint 2.0.0rc on the X4 Pro emulator.

The screen is a 480x800 portrait panel served by the browser bridge
(`/api/state`, `/api/tap`, `/screen.png`).  Raw GT911 contacts land on portrait
pixels unchanged (probe_x4pro_guest_touch_ui.x4pro_raw_to_portrait), so the
coordinates below are raw touch points.

Coordinate derivation (firmware, CrossPoint 2.0.0rc, stock Lyra theme)
----------------------------------------------------------------------
Home menu rows are drawn from `menuTop = homeTopPadding + homeCoverTileHeight
+ homeMenuTopOffset` with `rowStep = getMenuRowHeight() + menuSpacing`
(HomeActivity.cpp:436-443).  The running theme is Lyra (CrossPointSettings.h:336
defaults `uiTheme = LYRA`), whose metrics are 56 + 242 + 16 = 314 px and
64 + 8 = 72 px (LyraTheme.h:35-36,41-46; BaseTheme.cpp:714).  The measured
dithered selection band starts at y=314 and every later row label is one 72 px
step lower, so row centres are 346/418/490/562.  `rowTouch` accepts the whole
row width (HomeActivity.cpp:443-444, MappedInputManager.cpp:251-268); the plan
taps x=240, the horizontal middle.

Pushed screens draw a tappable header-back control at `band.x + 4, band.y + 4`
(BaseTheme.cpp:439-445, components/HeaderBackTapTarget.h:11-27).  The measured
arrow glyph is x24..44, y54..73 on the SD card, Library and Settings screens;
the plan taps (36, 60), inside the glyph.

Frame evidence (measured live on 2026-10-31, QEMU 9.2.2 built in
qemu-wf-radio-20261031 (source HEAD febae182), firmware
crosspoint-2.0.0rc-x4pro-flash-16m.bin 2be70081...)
-----------------------------------------------------------------
Every SHA below is the SHA-256 of the genuine SSD1677 visible-RAM frame read
from `/api/state`, and each was cross-checked against the served `/screen.png`
by inverting the portrait transform (`served_frame_sha256`).  The test run that
produced them is recorded in experiments/0082-crosspoint-navigation-20261031.md.

Home remembers the focused row, so returning from Library or Settings lands on
a Home frame with that row highlighted and NOT on the initial Home frame
(78e74345...).  Opening a row first repaints Home with the new highlight
(4b3ab700... / 703980c3...), then pushes the activity.  These intermediate
frames are recorded, not gated; the gate is the settled screen SHA.

Gates
-----
A step passes only when the frame SHA changed, the refresh counter advanced,
the guest was ready before the tap, the starting SHA is the planned one
(strict order), and the settled SHA is the planned target.  A tap that does not
change the frame always fails with `panel_frame_unchanged` - there is no mode
that relaxes that rule.  `--allow-sha-drift` only downgrades the target-SHA
comparison (recorded as drift), never the change gate.

The browser channel has no GT911 I2C counters, so every verdict reports
`full_acceptance: false`; it proves the panel changed, not that the guest read
the touch point over I2C.  The Home gesture is a bottom-edge up swipe
(MappedInputManager.cpp:365-367, ActivityManager.cpp:120-126) and the bridge
can only inject a stationary tap, so Home-as-gesture is unreachable here; Back
is proven through the header arrow.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from epd_frame_io import ssd1677_landscape_to_portrait
from x4pro_guest_navigation import portrait_from_mono_png

PANEL_WIDTH = 480
PANEL_HEIGHT = 800
PANEL_BYTES = PANEL_WIDTH * PANEL_HEIGHT // 8

# Measured live on the CrossPoint 2.0.0rc image; see the module docstring.
HOME_BROWSE_SELECTED = "78e743450b8f67ced89eb59d86b7c9da61eb306b4c3e92c31f50b345ad99c296"
BROWSE_FILES = "24a14c7230b583c7ca042105b7da170061dc819bf61516696ca5d2d5cd7a8a89"
HOME_LIBRARY_SELECTED = "4b3ab70080b6027338782995d0e1225183fbfc62fc718869dacef9bed7abbc5a"
LIBRARY_INDEX_POPUP = "5511a8302b14fd4a145a1d5dad47dd2c90079bb3110d4cb92387655abc56ade9"
LIBRARY = "f2b8f94d389fa4abe89dc701c522dd9a4010e26d45b866a3816f3ea27f9d8b13"
HOME_SETTINGS_SELECTED = "703980c39d79a8f2dc39242895e01e628b620663ff94d26ace3d1e7ca119d70b"
SETTINGS = "148be125d47d446775b487c0a75dd26acc94291f7ad5d1a4366ca6f98cc77636"

MEASURED_FIRMWARE_SHA256 = "2be70081d645ab1df57e28319f010557e243ef4e3eb06190731f56f91f981bbe"

SCREEN_NAMES = {
    HOME_BROWSE_SELECTED: "home-browse-selected",
    BROWSE_FILES: "browse-files",
    HOME_LIBRARY_SELECTED: "home-library-selected",
    LIBRARY_INDEX_POPUP: "library-index-popup",
    LIBRARY: "library",
    HOME_SETTINGS_SELECTED: "home-settings-selected",
    SETTINGS: "settings",
}


def screen_name(sha: str | None) -> str:
    if not sha:
        return "unknown"
    return SCREEN_NAMES.get(sha, "unknown")


TARGETS: dict[str, dict] = {
    "home-browse-files": {
        "raw_xy": (240, 346),
        "label": "Browse Files row",
        "evidence": "Lyra row 0: 314..377 (HomeActivity.cpp:436-443, "
                    "LyraTheme.h:35-36,41-46); measured band starts y=314",
    },
    "home-library": {
        "raw_xy": (240, 418),
        "label": "Library row",
        "evidence": "Lyra row 1 centre: 314 + 72 + 32 = 418; measured label "
                    "ink y406..433",
    },
    "home-settings": {
        "raw_xy": (240, 562),
        "label": "Settings row",
        "evidence": "Lyra row 3 centre: 314 + 3*72 + 32 = 562; measured label "
                    "ink y550..577",
    },
    "header-back": {
        "raw_xy": (36, 60),
        "label": "header back arrow",
        "evidence": "measured arrow glyph x24..44 y54..73 on the SD card, "
                    "Library and Settings screens; BaseTheme.cpp:439-445",
    },
}


def _step(name, target, screen_before, screen_after, sha_before, sha_after,
          transients=()):
    return {
        "name": name,
        "target": target,
        "expect_screen_before": screen_before,
        "expect_screen_after": screen_after,
        "expect_frame_sha256_before": sha_before,
        "expect_frame_sha256_after": sha_after,
        # Frames measured between the tap and the settled screen; recorded as
        # evidence, never accepted as the step target.
        "observed_transients": list(transients),
    }


SEQUENCES: dict[str, list[dict]] = {
    # Measured end-to-end after /api/reboot on the 8794 instance.  It is the
    # canonical proof because every step starts from the previous settled frame.
    "home-tour": [
        _step("open-browse-files", "home-browse-files",
              "home-browse-selected", "browse-files",
              HOME_BROWSE_SELECTED, BROWSE_FILES),
        _step("back-from-browse-files", "header-back",
              "browse-files", "home-browse-selected",
              BROWSE_FILES, HOME_BROWSE_SELECTED),
        _step("open-library", "home-library",
              "home-browse-selected", "library",
              HOME_BROWSE_SELECTED, LIBRARY,
              transients=(HOME_LIBRARY_SELECTED, LIBRARY_INDEX_POPUP)),
        _step("back-from-library", "header-back",
              "library", "home-library-selected",
              LIBRARY, HOME_LIBRARY_SELECTED),
        _step("open-settings", "home-settings",
              "home-library-selected", "settings",
              HOME_LIBRARY_SELECTED, SETTINGS,
              transients=(HOME_SETTINGS_SELECTED,)),
        _step("back-from-settings", "header-back",
              "settings", "home-settings-selected",
              SETTINGS, HOME_SETTINGS_SELECTED),
    ],
    # Fragments of the tour, each with the same measured frames.
    "browse-files-and-back": [
        _step("open-browse-files", "home-browse-files",
              "home-browse-selected", "browse-files",
              HOME_BROWSE_SELECTED, BROWSE_FILES),
        _step("back-from-browse-files", "header-back",
              "browse-files", "home-browse-selected",
              BROWSE_FILES, HOME_BROWSE_SELECTED),
    ],
    "library-and-back": [
        _step("open-library", "home-library",
              "home-browse-selected", "library",
              HOME_BROWSE_SELECTED, LIBRARY,
              transients=(HOME_LIBRARY_SELECTED, LIBRARY_INDEX_POPUP)),
        _step("back-from-library", "header-back",
              "library", "home-library-selected",
              LIBRARY, HOME_LIBRARY_SELECTED),
    ],
    # Starts on Home with Library focused, because the measured open-settings
    # step ran from there in the tour.
    "settings-and-back": [
        _step("open-settings", "home-settings",
              "home-library-selected", "settings",
              HOME_LIBRARY_SELECTED, SETTINGS,
              transients=(HOME_SETTINGS_SELECTED,)),
        _step("back-from-settings", "header-back",
              "settings", "home-settings-selected",
              SETTINGS, HOME_SETTINGS_SELECTED),
    ],
}

DEFAULT_SEQUENCE = "home-tour"


def resolve_sequence(name: str) -> list[dict]:
    try:
        return [dict(step, observed_transients=list(step["observed_transients"]))
                for step in SEQUENCES[name]]
    except KeyError:
        raise ValueError(f"unknown sequence {name!r}; choose from "
                         f"{', '.join(sorted(SEQUENCES))}") from None


def build_plan(name: str = DEFAULT_SEQUENCE) -> list[dict]:
    """Flatten one measured sequence into a strictly chained step plan.

    Rejects a plan whose frames do not chain, whose SHA is malformed, or whose
    step would expect no visible change - such a plan could not gate anything.
    """
    steps = resolve_sequence(name)
    for index, step in enumerate(steps):
        step["index"] = index
        x, y = TARGETS[step["target"]]["raw_xy"]
        step["raw_xy"] = [x, y]
        for key in ("expect_frame_sha256_before", "expect_frame_sha256_after"):
            if not _valid_sha(step[key]):
                raise ValueError(f"step {step['name']!r} has a malformed {key}")
        if step["expect_frame_sha256_before"] == step["expect_frame_sha256_after"]:
            raise ValueError(f"step {step['name']!r} expects no visible change")
        if index:
            previous = steps[index - 1]
            if step["expect_frame_sha256_before"] != previous["expect_frame_sha256_after"]:
                raise ValueError(f"step {step['name']!r} does not chain from "
                                 f"{previous['name']!r}")
            if step["expect_screen_before"] != previous["expect_screen_after"]:
                raise ValueError(f"screen names do not chain at {step['name']!r}")
    return steps


def initial_frame_sha256(name: str = DEFAULT_SEQUENCE) -> str:
    return resolve_sequence(name)[0]["expect_frame_sha256_before"]


def _valid_sha(value) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _frames(state: dict):
    frames = state.get("frames")
    return frames if type(frames) is int else None


def assess_step(step: dict, before: dict, after: dict) -> dict:
    """Gate one already-executed step on real frame state.

    `panel_frame_unchanged` is unconditional: a tap that leaves the panel
    byte-identical is never a success, even when `--allow-sha-drift` turned the
    target-frame comparison into a recorded observation.
    """
    reasons: list[str] = []
    before_sha = before.get("sha256") if _valid_sha(before.get("sha256")) else None
    after_sha = after.get("sha256") if _valid_sha(after.get("sha256")) else None
    if not before_sha:
        reasons.append("missing_frame_sha256_before")
    if not after_sha:
        reasons.append("missing_frame_sha256_after")
    if before.get("ready") is not True:
        reasons.append("guest_not_ready")
    expected_before = step["expect_frame_sha256_before"]
    expected_after = step["expect_frame_sha256_after"]
    if before_sha and before_sha != expected_before:
        reasons.append("precondition_frame_sha256_mismatch")
    frames_before, frames_after = _frames(before), _frames(after)
    if frames_before is not None and frames_after is not None and frames_after <= frames_before:
        reasons.append("frames_not_advanced")
    changed = bool(before_sha and after_sha and before_sha != after_sha)
    if before_sha and after_sha and not changed:
        reasons.append("panel_frame_unchanged")
    if after_sha and after_sha != expected_after:
        reasons.append("target_frame_sha256_mismatch")
    return {
        "step": step["name"],
        "target": step["target"],
        "raw_xy": list(TARGETS[step["target"]]["raw_xy"]),
        "expected_screen_before": step["expect_screen_before"],
        "expected_screen_after": step["expect_screen_after"],
        "screen_before": screen_name(before_sha),
        "screen_after": screen_name(after_sha),
        "frame_sha256_before": before_sha,
        "frame_sha256_after": after_sha,
        "expected_frame_sha256_before": expected_before,
        "expected_frame_sha256_after": expected_after,
        "frames_before": frames_before,
        "frames_after": frames_after,
        "frame_changed": changed,
        "passed": not reasons,
        "failure_codes": reasons,
    }


def validate_sequence(plan: list[dict], observed: list[dict]) -> dict:
    """Strict-order check of the observed chain against the planned chain.

    An observed step may not be renamed, reordered, skipped, or start/land on a
    frame the plan did not predict.  Incomplete runs are rejected, never
    silently truncated into a pass.
    """
    if not plan:
        return {"ok": False, "reason": "empty_plan"}
    if not observed:
        return {"ok": False, "reason": "no_observed_steps"}
    if len(observed) > len(plan):
        return {"ok": False, "reason": "more_steps_than_planned",
                "observed_steps": len(observed), "planned_steps": len(plan)}
    for index, item in enumerate(observed):
        expected = plan[index]
        if item.get("step") != expected["name"]:
            return {"ok": False, "step": item.get("step"),
                    "reason": f"step_{index}_is_{item.get('step')}_not_"
                              f"{expected['name']}"}
        if item.get("frame_sha256_before") != expected["expect_frame_sha256_before"]:
            return {"ok": False, "step": expected["name"],
                    "reason": f"step_{index}_started_on_unplanned_frame"}
        if item.get("frame_sha256_after") != expected["expect_frame_sha256_after"]:
            return {"ok": False, "step": expected["name"],
                    "reason": f"step_{index}_reached_unplanned_frame"}
    if len(observed) < len(plan):
        return {"ok": False, "reason": f"sequence_incomplete_after_"
                                       f"{len(observed)}_of_{len(plan)}_steps"}
    return {"ok": True, "steps_checked": len(observed)}


def portrait_to_landscape(portrait: bytes, width: int, height: int) -> bytes:
    """Invert epd_frame_io.ssd1677_landscape_to_portrait for a PNG readout.

    The bridge hashes the landscape SSD1677 RAM but serves the portrait PNG;
    this recomputes the same landscape bytes so a captured PNG can be checked
    against the frame SHA the verdict quotes.
    """
    if width % 8 or height % 8 or len(portrait) != width * height // 8:
        raise ValueError("portrait frame has invalid dimensions")
    stride = width // 8
    landscape_stride = height // 8
    out = bytearray(b"\xff" * (width * height // 8))
    for row in range(height):
        base = row * stride
        for column_byte in range(stride):
            byte = portrait[base + column_byte]
            if byte == 0xff:
                continue
            for bit in range(8):
                if byte & (0x80 >> bit):
                    continue
                column = column_byte * 8 + bit
                out[column * landscape_stride + row // 8] &= ~(0x80 >> (row % 8))
    return bytes(out)


def served_frame_sha256(png: bytes) -> str:
    width, height, portrait = portrait_from_mono_png(png)
    if (width, height) != (PANEL_WIDTH, PANEL_HEIGHT):
        raise ValueError(f"served PNG is {width}x{height}, expected "
                         f"{PANEL_WIDTH}x{PANEL_HEIGHT}")
    if len(portrait) != PANEL_BYTES:
        raise ValueError("served PNG does not decode to a packed 1bpp frame")
    return hashlib.sha256(portrait_to_landscape(portrait, width, height)).hexdigest()


def is_loopback_url(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return host in ("127.0.0.1", "::1", "localhost")


def validate_base_url(url: str | None) -> str:
    if not url or not isinstance(url, str):
        raise ValueError("base URL is required")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"base URL must be absolute http(s): {url!r}")
    return url.rstrip("/")


class BrowserBridge:
    """The only code here that touches a socket; every call is a real HTTP GET/POST."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = validate_base_url(base_url)
        self.timeout = timeout

    def _get(self, path: str) -> bytes:
        with urllib.request.urlopen(self.base_url + path, timeout=self.timeout) as response:
            return response.read()

    def _post(self, path: str, payload: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            self.base_url + path, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read())

    def state(self) -> dict:
        return json.loads(self._get("/api/state"))

    def screen_png(self) -> bytes:
        return self._get("/screen.png")

    def tap(self, x: int, y: int, duration_ms: int = 150) -> dict:
        return self._post("/api/tap", {"x": x, "y": y, "duration_ms": duration_ms})

    def reboot(self) -> dict:
        return self._post("/api/reboot")


def _settle(bridge, *, timeout: float, polls: int, poll_interval: float) -> dict:
    """Read state until (frames, sha256) is unchanged for `polls` reads."""
    deadline = time.monotonic() + timeout
    last = None
    stable = 0
    state = bridge.state()
    while True:
        key = (state.get("frames"), state.get("sha256"))
        if key == last:
            stable += 1
            if stable >= polls:
                return state
        else:
            last = key
            stable = 0
        if time.monotonic() >= deadline:
            return state
        time.sleep(poll_interval)
        state = bridge.state()


def _poll_for_change(bridge, step: dict, before: dict, *, require_target: bool,
                     timeout: float, poll_interval: float):
    """Poll after a tap until the planned target (or any change) is visible."""
    target = step["expect_frame_sha256_after"]
    before_sha = before.get("sha256")
    before_frames = _frames(before)
    intermediate: list[str] = []
    latest = before
    timed_out = False
    deadline = time.monotonic() + timeout
    while True:
        latest = bridge.state()
        sha = latest.get("sha256")
        frames = _frames(latest)
        advanced = (frames is not None and before_frames is not None
                    and frames > before_frames)
        if (advanced and isinstance(sha, str) and sha not in (before_sha, target)
                and sha not in intermediate):
            intermediate.append(sha)
        if advanced and (sha == target if require_target else sha != before_sha):
            return latest, intermediate, False
        if time.monotonic() >= deadline:
            timed_out = True
            return latest, intermediate, True
        time.sleep(poll_interval)


def _firmware_sha(state: dict) -> str | None:
    firmware = state.get("firmware")
    if not isinstance(firmware, dict):
        return None
    sha = firmware.get("sha256")
    return sha if _valid_sha(sha) else None


def run_plan(plan: list[dict], bridge, *, sequence: str, out_dir: Path | None = None,
             step_timeout: float = 25.0, settle_seconds: float = 8.0,
             settle_polls: int = 3, poll_interval: float = 0.4,
             reboot_first: bool = False, allow_sha_drift: bool = False,
             allow_firmware_drift: bool = False, verify_png: bool = True) -> dict:
    """Execute the plan in order, stopping at the first failed gate.

    No tap is sent before the preconditions pass.  The returned verdict is the
    only pass path: `navigation_sequence_verified` requires every planned step,
    the strict order check and at least one injected tap per step.
    """
    if not plan:
        raise ValueError("plan must not be empty")
    out_dir = out_dir or Path("cp-nav-evidence")
    out_dir.mkdir(parents=True, exist_ok=True)

    taps = 0
    results: list[dict] = []
    observed: list[dict] = []
    aborted: str | None = None

    state = bridge.state()
    if reboot_first:
        bridge.reboot()
        deadline = time.monotonic() + max(45.0, step_timeout)
        while True:
            state = bridge.state()
            if (state.get("ready") is True
                    and state.get("sha256") == plan[0]["expect_frame_sha256_before"]):
                break
            if time.monotonic() >= deadline:
                aborted = "initial_frame_not_reached_after_reboot"
                break
            time.sleep(poll_interval)

    firmware_observed = _firmware_sha(state)
    firmware_ok = firmware_observed == MEASURED_FIRMWARE_SHA256
    if not aborted and not firmware_ok and not allow_firmware_drift:
        aborted = "firmware_sha256_mismatch"
    if not aborted and state.get("ready") is not True:
        aborted = "guest_not_ready"
    if not aborted and state.get("sha256") != plan[0]["expect_frame_sha256_before"]:
        aborted = "precondition_frame_sha256_mismatch"

    for step in plan:
        if aborted:
            break
        before = state
        response = bridge.tap(*step["raw_xy"])
        taps += 1
        if response.get("accepted") is not True:
            aborted = "tap_not_accepted"
            results.append({"step": step["name"], "passed": False,
                            "failure_codes": [aborted], "tap_response": response})
            break
        _latest, intermediate, timed_out = _poll_for_change(
            bridge, step, before, require_target=not allow_sha_drift,
            timeout=step_timeout, poll_interval=poll_interval)
        png = bridge.screen_png()
        after = _settle(bridge, timeout=settle_seconds, polls=settle_polls,
                        poll_interval=poll_interval)
        result = assess_step(step, before, after)
        result["intermediate_frame_sha256s"] = intermediate
        result["intermediate_screens"] = [screen_name(sha) for sha in intermediate]
        if timed_out:
            result["failure_codes"].append("target_frame_not_reached_within_timeout")
        png_path = out_dir / f"{step['index']:02d}-{step['name']}.png"
        png_path.write_bytes(png)
        result["png"] = str(png_path)
        if verify_png:
            try:
                png_sha = served_frame_sha256(png)
            except ValueError as exc:
                result["png_matches_state"] = False
                result["png_error"] = str(exc)
                result["failure_codes"].append("evidence_png_sha256_mismatch")
            else:
                result["png_frame_sha256"] = png_sha
                result["png_matches_state"] = png_sha == after.get("sha256")
                if not result["png_matches_state"]:
                    result["failure_codes"].append("evidence_png_sha256_mismatch")
        if allow_sha_drift and "target_frame_sha256_mismatch" in result["failure_codes"]:
            result["failure_codes"].remove("target_frame_sha256_mismatch")
            result["target_sha256_gate"] = "drifted"
        else:
            result["target_sha256_gate"] = ("matched"
                                            if result["frame_sha256_after"]
                                            == step["expect_frame_sha256_after"]
                                            else "mismatched")
        result["passed"] = not result["failure_codes"]
        results.append(result)
        observed.append(result)
        if not result["passed"]:
            aborted = f"step_failed:{step['name']}"
            break
        state = after

    order_check = validate_sequence(plan, observed)
    all_passed = bool(results) and all(result["passed"] for result in results)
    verdict = {
        "sequence": sequence,
        "initial_frame_sha256": plan[0]["expect_frame_sha256_before"],
        "firmware": {
            "expected": MEASURED_FIRMWARE_SHA256,
            "observed": firmware_observed,
            "gate": "matched" if firmware_ok else "mismatched",
        },
        "steps_run": len(results),
        "steps_planned": len(plan),
        "steps_passed": sum(1 for result in results if result["passed"]),
        "taps_injected": taps,
        "first_failing_step": next((result["step"] for result in results
                                    if not result["passed"]), None),
        "aborted": aborted,
        "frame_sha256_chain": ([plan[0]["expect_frame_sha256_before"]]
                               + [result["frame_sha256_after"] for result in results]),
        "strict_order_ok": order_check["ok"],
        "order_check": order_check,
        "navigation_sequence_verified": bool(all_passed and not aborted
                                            and len(results) == len(plan)
                                            and order_check["ok"]),
        "reached_screen": results[-1]["screen_after"] if results else None,
        "guest_gt911_i2c_consumption_observed": False,
        "full_acceptance": False,
        "limits": [
            "browser channel: no GT911 I2C point-read/status-clear counters",
            "Home gesture is a bottom-edge up swipe; /api/tap cannot inject it",
        ],
    }
    return {
        "plan": plan,
        "steps": results,
        "verdict": verdict,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the measured CrossPoint 2.0.0rc tap plan against one "
                    "browser bridge instance and gate every step on real frame SHAs.")
    parser.add_argument("--base-url", required=True,
                        help="browser bridge origin, e.g. http://127.0.0.1:8794")
    parser.add_argument("--sequence", default=DEFAULT_SEQUENCE,
                        choices=sorted(SEQUENCES))
    parser.add_argument("--out", required=True, type=Path,
                        help="verdict JSON path; per-step PNGs go beside it")
    parser.add_argument("--step-timeout", type=float, default=25.0)
    parser.add_argument("--settle-seconds", type=float, default=8.0)
    parser.add_argument("--settle-polls", type=int, default=3)
    parser.add_argument("--poll-interval", type=float, default=0.4)
    parser.add_argument("--reboot-first", action="store_true",
                        help="POST /api/reboot and wait for the measured initial frame")
    parser.add_argument("--allow-sha-drift", action="store_true",
                        help="record a differing target frame instead of failing; "
                             "the changed-frame gate stays hard")
    parser.add_argument("--allow-firmware-drift", action="store_true",
                        help="run even when the bridge reports another firmware SHA")
    parser.add_argument("--no-verify-png", action="store_true",
                        help="skip checking the served PNG against the state SHA")
    parser.add_argument("--allow-loopback", action="store_true",
                        help="allow a loopback base URL (a second deliberate choice)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        validate_base_url(args.base_url)
    except ValueError as exc:
        parser.error(str(exc))
    if is_loopback_url(args.base_url) and not args.allow_loopback:
        parser.error("loopback base URL requires --allow-loopback")
    plan = build_plan(args.sequence)
    bridge = BrowserBridge(args.base_url)
    try:
        report = run_plan(
            plan, bridge, sequence=args.sequence, out_dir=args.out.parent,
            step_timeout=args.step_timeout, settle_seconds=args.settle_seconds,
            settle_polls=args.settle_polls, poll_interval=args.poll_interval,
            reboot_first=args.reboot_first, allow_sha_drift=args.allow_sha_drift,
            allow_firmware_drift=args.allow_firmware_drift,
            verify_png=not args.no_verify_png)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        report = {"plan": plan, "steps": [], "verdict": {
            "sequence": args.sequence,
            "navigation_sequence_verified": False,
            "aborted": "bridge_unreachable",
            "error": str(exc),
            "taps_injected": 0,
            "guest_gt911_i2c_consumption_observed": False,
            "full_acceptance": False,
        }}
    args.out.write_text(json.dumps(report, indent=1) + "\n")
    verdict = report["verdict"]
    print(json.dumps({key: verdict.get(key) for key in (
        "sequence", "navigation_sequence_verified", "steps_passed",
        "steps_planned", "taps_injected", "first_failing_step", "aborted",
        "strict_order_ok", "reached_screen")}, indent=1))
    if verdict.get("navigation_sequence_verified") is True:
        return 0
    if verdict.get("aborted") == "bridge_unreachable":
        return 3
    return 1


if __name__ == "__main__":
    sys.exit(main())
