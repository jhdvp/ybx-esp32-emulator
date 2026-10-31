#!/usr/bin/env python3
"""Acceptance runner for the measured stock CrossPoint 2.0.0rc tap plan.

This is the committed HTTP runner in the style of run_x4pro_guest_navigation.py
(browser channel only).  It drives one `serve_x4pro_browser.py`-compatible
instance over /api/state, /screen.png, /api/tap, /api/reboot and
/api/firmware, records a PNG and the SSD1677 frame SHA for every step, and
writes a machine-readable JSON verdict.  There is no default URL: --base-url is
required, and a loopback URL needs the second deliberate choice
--allow-loopback, so the tool can never fall onto the orchestrator's live
container by accident.

Every step is gated on the real panel, measured live on 2026-10-31 and
recorded in experiments/0082-crosspoint-navigation-20261031.md:

- a tap that leaves the panel byte-identical fails (`panel_frame_unchanged`);
  no flag relaxes this;
- a guest that is not ready fails before any input is sent (`guest_not_ready`);
- a frame that does not match the planned chain fails (`out-of-order` and the
  strict order check reject renamed, reordered, skipped or extra steps);
- a missing or malformed expected SHA is rejected when the plan is built, and
  a missing readout from the instance fails the step loudly
  (`missing_frame_sha256_before/after`) instead of falling back to an
  assumption;
- each captured PNG is inverted back to landscape RAM and re-hashed; evidence
  that disagrees with /api/state fails (`evidence_png_sha256_mismatch`).

`--allow-sha-drift` only downgrades the settled-target comparison to a recorded
`target_sha256_gate: drifted`; the changed-frame, readiness and ordering gates
stay hard.  `--allow-firmware-drift` is the only way to run against an image
other than the measured `2be70081...`, and the drift is recorded in the verdict.

The browser bridge exposes no GT911 I2C point-read/status-clear counters, so
every verdict carries `full_acceptance: false`: this proves the guest repainted
in reaction to the injected contact, not that the guest read the point over
I2C.  Exit codes: 0 verified, 1 failed or refused, 2 bad arguments, 3 instance
unreachable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
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

# Measured live 2026-10-31 on qemu-wf-radio-20261031 (QEMU 9.2.2), firmware
# crosspoint-2.0.0rc-x4pro-flash-16m.bin (2be70081...), ROM
# s3-full-rom-generator-test.bin (5b950be9...), SD
# qa-20261029/ybx-test-sd-fat16-mbr-pristine.img.  Each SHA is the SHA-256 of
# the genuine SSD1677 visible landscape RAM from /api/state and every one was
# cross-checked against the served /screen.png (experiments/0082, measured
# frames table).
HOME_BROWSE_SELECTED = "78e743450b8f67ced89eb59d86b7c9da61eb306b4c3e92c31f50b345ad99c296"
BROWSE_FILES = "24a14c7230b583c7ca042105b7da170061dc819bf61516696ca5d2d5cd7a8a89"
HOME_LIBRARY_SELECTED = "4b3ab70080b6027338782995d0e1225183fbfc62fc718869dacef9bed7abbc5a"
LIBRARY_INDEX_POPUP = "5511a8302b14fd4a145a1d5dad47dd2c90079bb3110d4cb92387655abc56ade9"
LIBRARY = "f2b8f94d389fa4abe89dc701c522dd9a4010e26d45b866a3816f3ea27f9d8b13"
HOME_SETTINGS_SELECTED = "703980c39d79a8f2dc39242895e01e628b620663ff94d26ace3d1e7ca119d70b"
SETTINGS = "148be125d47d446775b487c0a75dd26acc94291f7ad5d1a4366ca6f98cc77636"

MEASURED_FIRMWARE_SHA256 = ("2be70081d645ab1df57e28319f010557e243ef4e3eb06190731f56f91f981bbe")
MEASURED_FIRMWARE_NAME = "crosspoint-2.0.0rc-x4pro-flash-16m.bin"

SCREEN_NAMES = {
    HOME_BROWSE_SELECTED: "home-browse-selected",
    BROWSE_FILES: "browse-files",
    HOME_LIBRARY_SELECTED: "home-library-selected",
    LIBRARY_INDEX_POPUP: "library-index-popup",
    LIBRARY: "library",
    HOME_SETTINGS_SELECTED: "home-settings-selected",
    SETTINGS: "settings",
}

# Raw GT911 portrait points; the bridge injects them unchanged
# (probe_x4pro_guest_touch_ui.x4pro_raw_to_portrait).  Derivations are quoted
# from the CrossPoint 2.0.0rc firmware in experiments/0082 (Lyra Home metrics
# menuTop=314, rowStep=72: HomeActivity.cpp:436-443, LyraTheme.h:35-36,41-46;
# header back at band.x+4, band.y+4: BaseTheme.cpp:439-445).
TARGETS: dict[str, dict] = {
    "home-browse-files": {
        "raw_xy": (240, 346),
        "label": "Browse Files row",
        "evidence": "Lyra row 0 centre 314+32=346; measured band starts y=314",
    },
    "home-library": {
        "raw_xy": (240, 418),
        "label": "Library row",
        "evidence": "Lyra row 1 centre 314+72+32=418; measured label ink y406..433",
    },
    "home-settings": {
        "raw_xy": (240, 562),
        "label": "Settings row",
        "evidence": "Lyra row 3 centre 314+3*72+32=562; measured label ink y550..577",
    },
    "header-back": {
        "raw_xy": (36, 60),
        "label": "header back arrow",
        "evidence": "measured arrow glyph x24..44 y54..73 on all pushed screens",
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
    # The canonical six-step tour, measured end-to-end after /api/reboot in
    # experiments/0082.  Every step starts from the previous settled frame.
    # Home remembers the focused row, so Back lands on the highlighted-Home
    # frames, not on the initial Home frame.
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


def screen_name(sha: str | None) -> str:
    if not sha:
        return "unknown"
    return SCREEN_NAMES.get(sha, "unknown")


def _valid_sha(value) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def build_plan(name: str = DEFAULT_SEQUENCE) -> list[dict]:
    """Flatten one measured sequence into a strictly chained step plan.

    Rejects a plan whose frames do not chain, whose expected SHA is missing or
    malformed, or whose step expects no visible change - such a plan could not
    gate anything, so it is refused before a single request is made.
    """
    try:
        steps = [dict(step, observed_transients=list(step["observed_transients"]))
                 for step in SEQUENCES[name]]
    except KeyError:
        raise ValueError(f"unknown sequence {name!r}; choose from "
                         f"{', '.join(sorted(SEQUENCES))}") from None
    for index, step in enumerate(steps):
        step["index"] = index
        if step["target"] not in TARGETS:
            raise ValueError(f"step {step['name']!r} targets unknown {step['target']!r}")
        x, y = TARGETS[step["target"]]["raw_xy"]
        step["raw_xy"] = [x, y]
        for key in ("expect_frame_sha256_before", "expect_frame_sha256_after"):
            if not _valid_sha(step[key]):
                raise ValueError(f"step {step['name']!r} has a missing or "
                                 f"malformed {key}")
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


def assess_step(step: dict, before: dict, after: dict) -> dict:
    """Gate one already-executed step on the real frame state.

    `panel_frame_unchanged` is unconditional: a tap that leaves the panel
    byte-identical is never a success.  A missing readout fails as
    `missing_frame_sha256_*`; it is never replaced by the recorded expectation.
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
    if before_sha and before_sha != step["expect_frame_sha256_before"]:
        reasons.append("precondition_frame_sha256_mismatch")
    frames_before = before.get("frames")
    frames_after = after.get("frames")
    if type(frames_before) is int and type(frames_after) is int \
            and frames_after <= frames_before:
        reasons.append("frames_not_advanced")
    changed = bool(before_sha and after_sha and before_sha != after_sha)
    if before_sha and after_sha and not changed:
        reasons.append("panel_frame_unchanged")
    if after_sha and after_sha != step["expect_frame_sha256_after"]:
        reasons.append("target_frame_sha256_mismatch")
    return {
        "step": step["name"],
        "target": step["target"],
        "raw_xy": list(step["raw_xy"]),
        "expected_screen_before": step["expect_screen_before"],
        "expected_screen_after": step["expect_screen_after"],
        "screen_before": screen_name(before_sha),
        "screen_after": screen_name(after_sha),
        "frame_sha256_before": before_sha,
        "frame_sha256_after": after_sha,
        "expected_frame_sha256_before": step["expect_frame_sha256_before"],
        "expected_frame_sha256_after": step["expect_frame_sha256_after"],
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
    against the frame SHA the verdict quotes.  Same mapping as
    ssd1677_landscape_to_portrait: destination X = source Y, a cleared bit is
    ink.
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
            if byte == 0xFF:
                continue
            for bit in range(8):
                if byte & (0x80 >> bit):
                    continue
                column = column_byte * 8 + bit
                out[column * landscape_stride + row // 8] &= ~(0x80 >> (row % 8))
    return bytes(out)


def served_frame_sha256(png: bytes) -> str:
    """Frame SHA recomputed from the served portrait PNG (round-trip check)."""
    width, height, portrait = portrait_from_mono_png(png)
    if (width, height) != (PANEL_WIDTH, PANEL_HEIGHT):
        raise ValueError(f"served PNG is {width}x{height}, expected "
                         f"{PANEL_WIDTH}x{PANEL_HEIGHT}")
    if len(portrait) != PANEL_BYTES:
        raise ValueError("served PNG does not decode to a packed 1bpp frame")
    return hashlib.sha256(portrait_to_landscape(portrait, width, height)).hexdigest()


def validate_base_url(url) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("--base-url must be an explicit non-empty http(s) URL; "
                         "this tool never guesses a running instance")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"--base-url {url!r} is not an absolute http(s) URL")
    return url.rstrip("/")


def is_loopback_url(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return host in ("127.0.0.1", "::1", "localhost")


class BridgeError(RuntimeError):
    pass


class HttpTransport:
    """Minimal urllib transport; the only place this runner touches a socket."""

    def __init__(self, base_url: str, timeout: float):
        self.base_url = validate_base_url(base_url)
        self.timeout = timeout

    def _open(self, request):
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                body = response.read()
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise BridgeError(f"instance unreachable: {exc}") from exc
        return status, body

    def get_json(self, path: str) -> dict:
        status, body = self._open(urllib.request.Request(self.base_url + path,
                                                          method="GET"))
        if status != 200:
            raise BridgeError(f"GET {path} -> HTTP {status}: {body[:200]!r}")
        try:
            return json.loads(body)
        except ValueError as exc:
            raise BridgeError(f"GET {path} returned non-JSON: {body[:200]!r}") from exc

    def get_bytes(self, path: str) -> bytes:
        status, body = self._open(urllib.request.Request(self.base_url + path,
                                                          method="GET"))
        if status != 200:
            raise BridgeError(f"GET {path} -> HTTP {status}")
        return body

    def post_json(self, path: str, payload: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload).encode()
        if data is not None and len(data) > 160:
            raise BridgeError("request payload exceeds the bridge's 160-byte limit")
        request = urllib.request.Request(self.base_url + path, data=data, method="POST",
                                          headers={"Content-Type": "application/json"})
        status, body = self._open(request)
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"raw": body[:200]}
        if status != 200:
            raise BridgeError(f"POST {path} -> HTTP {status}: {parsed}")
        return parsed


STATE_KEYS = ("frames", "ready", "sha256", "firmware")


def bridge_state(transport) -> dict:
    """Read the instance's report of the real panel; never fabricate it."""
    state = transport.get_json("/api/state")
    for key in STATE_KEYS:
        if key not in state:
            raise BridgeError(f"/api/state response is missing {key!r}: {state!r}")
    return state


def bridge_firmware_sha(state: dict) -> str | None:
    firmware = state.get("firmware")
    if not isinstance(firmware, dict):
        return None
    sha = firmware.get("sha256")
    return sha if _valid_sha(sha) else None


def capture(transport, out_dir: Path, label: str, *, verify_png: bool,
            state: dict | None = None) -> dict:
    """Snapshot the panel: counters, state SHA, served PNG and its re-hash."""
    state = state if state is not None else bridge_state(transport)
    png_bytes = transport.get_bytes("/screen.png")
    if not png_bytes:
        raise BridgeError("/screen.png returned an empty body; no frame to judge")
    png_path = out_dir / f"{label}.png"
    png_path.write_bytes(png_bytes)
    snapshot = dict(state)
    snapshot["png"] = str(png_path)
    snapshot["png_sha256"] = hashlib.sha256(png_bytes).hexdigest()
    snapshot["captured_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if verify_png:
        try:
            snapshot["png_frame_sha256"] = served_frame_sha256(png_bytes)
        except ValueError as exc:
            snapshot["png_error"] = str(exc)
    return snapshot


def poll_for_change(transport, step: dict, before: dict, *, require_target: bool,
                   timeout: float, poll_interval: float):
    """Poll after a tap until the planned target (or any change) is visible."""
    target = step["expect_frame_sha256_after"]
    before_sha = before.get("sha256")
    before_frames = before.get("frames")
    intermediate: list[str] = []
    latest = before
    deadline = time.monotonic() + timeout
    while True:
        latest = bridge_state(transport)
        sha = latest.get("sha256")
        frames = latest.get("frames")
        advanced = (type(frames) is int and type(before_frames) is int
                    and frames > before_frames)
        if advanced and isinstance(sha, str) and sha not in (before_sha, target) \
                and sha not in intermediate:
            intermediate.append(sha)
        if advanced and (sha == target if require_target else sha != before_sha):
            return latest, intermediate, False
        if time.monotonic() >= deadline:
            return latest, intermediate, True
        time.sleep(poll_interval)


def settle(transport, *, timeout: float, polls: int, poll_interval: float) -> dict:
    """Read state until (frames, sha256) is unchanged for `polls` reads."""
    deadline = time.monotonic() + timeout
    last = None
    stable = 0
    state = bridge_state(transport)
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
        state = bridge_state(transport)


def run_plan(transport, plan: list[dict], *, sequence: str, out_dir: Path,
             step_timeout: float, settle_seconds: float, settle_polls: int,
             poll_interval: float, tap_duration_ms: int, reboot_first: bool,
             select_firmware: str | None, allow_sha_drift: bool,
             allow_firmware_drift: bool, verify_png: bool) -> dict:
    """Execute the plan in order and return the verdict; stop at the first gate.

    No tap is sent before the preconditions pass: firmware identity, guest
    readiness and the planned starting frame are all checked first.
    """
    if not plan:
        raise ValueError("plan must not be empty")
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    observed: list[dict] = []
    taps = 0
    aborted: str | None = None

    state = bridge_state(transport)
    if select_firmware:
        transport.post_json("/api/firmware", {"name": select_firmware})
        deadline = time.monotonic() + max(90.0, step_timeout)
        while bridge_firmware_sha(bridge_state(transport)) != MEASURED_FIRMWARE_SHA256:
            if time.monotonic() >= deadline:
                aborted = "firmware_switch_not_confirmed"
                break
            time.sleep(poll_interval)
        state = bridge_state(transport)
    if reboot_first and not aborted:
        transport.post_json("/api/reboot")
        deadline = time.monotonic() + max(60.0, step_timeout)
        while True:
            state = bridge_state(transport)
            if (state.get("ready") is True
                    and state.get("sha256") == plan[0]["expect_frame_sha256_before"]):
                break
            if time.monotonic() >= deadline:
                aborted = "initial_frame_not_reached_after_reboot"
                break
            time.sleep(poll_interval)

    firmware_observed = bridge_firmware_sha(state)
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
        before = capture(transport, out_dir, f"{step['index']:02d}-{step['name']}-before",
                         verify_png=False, state=state)
        response = transport.post_json("/api/tap", {"x": step["raw_xy"][0],
                                                    "y": step["raw_xy"][1],
                                                    "duration_ms": tap_duration_ms})
        taps += 1
        if response.get("accepted") is not True:
            aborted = "tap_not_accepted"
            results.append({"step": step["name"], "passed": False,
                            "failure_codes": [aborted], "tap_response": response})
            break
        latest, intermediate, timed_out = poll_for_change(
            transport, step, before, require_target=not allow_sha_drift,
            timeout=step_timeout, poll_interval=poll_interval)
        after = settle(transport, timeout=settle_seconds, polls=settle_polls,
                       poll_interval=poll_interval)
        after_shot = capture(transport, out_dir, f"{step['index']:02d}-{step['name']}-after",
                             verify_png=verify_png, state=after)
        result = assess_step(step, before, after)
        result["tap_http_accepted"] = response.get("accepted") is True
        result["intermediate_frame_sha256s"] = intermediate
        result["intermediate_screens"] = [screen_name(sha) for sha in intermediate]
        if timed_out:
            result["failure_codes"].append("target_frame_not_reached_within_timeout")
            result["passed"] = False
        result["png"] = after_shot["png"]
        result["png_sha256"] = after_shot["png_sha256"]
        if verify_png:
            if "png_error" in after_shot:
                result["png_matches_state"] = False
                result["png_error"] = after_shot["png_error"]
                result["failure_codes"].append("evidence_png_sha256_mismatch")
            else:
                result["png_frame_sha256"] = after_shot["png_frame_sha256"]
                result["png_matches_state"] = (after_shot["png_frame_sha256"]
                                               == after.get("sha256"))
                if not result["png_matches_state"]:
                    result["failure_codes"].append("evidence_png_sha256_mismatch")
            result["passed"] = not result["failure_codes"]
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
        "channel": "browser_http",
        "sequence": sequence,
        "initial_frame_sha256": plan[0]["expect_frame_sha256_before"],
        "firmware": {
            "expected": MEASURED_FIRMWARE_SHA256,
            "observed": firmware_observed,
            "gate": "matched" if firmware_ok else "mismatched",
        },
        "steps_passed": sum(1 for result in results if result["passed"]),
        "steps_planned": len(plan),
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
        "physical_touch_verified": False,
        "shared_server_used_by_default": False,
        "limits": [
            "browser channel: no GT911 I2C point-read/status-clear counters",
            "Home gesture is a bottom-edge up swipe; /api/tap cannot inject it",
        ],
    }
    return {"plan": plan, "steps": results, "verdict": verdict}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="the tap plan, measured frames and gates: "
               "experiments/0082-crosspoint-navigation-20261031.md")
    parser.add_argument("--base-url", required=True,
                        help="browser bridge origin, e.g. http://127.0.0.1:8795. "
                             "There is no default; the runner never guesses.")
    parser.add_argument("--allow-loopback", action="store_true",
                        help="permit a loopback base URL (only your own "
                             "instance; a deliberate second choice)")
    parser.add_argument("--sequence", default=DEFAULT_SEQUENCE,
                        choices=sorted(SEQUENCES))
    parser.add_argument("--out", type=Path, default=None,
                        help="verdict JSON path; per-step PNGs go beside it "
                             "(default: crosspoint-acceptance-<sequence>.json "
                             "under the current directory)")
    parser.add_argument("--step-timeout", type=float, default=25.0,
                        help="bounded wait for the settled target frame after a tap")
    parser.add_argument("--settle-seconds", type=float, default=8.0)
    parser.add_argument("--settle-polls", type=int, default=3)
    parser.add_argument("--poll-interval", type=float, default=0.4)
    parser.add_argument("--tap-duration-ms", type=int, default=150,
                        help="/api/tap hold time (the bridge allows 60..500)")
    parser.add_argument("--timeout-seconds", type=float, default=30.0,
                        help="per-request HTTP timeout")
    parser.add_argument("--reboot-first", action="store_true",
                        help="POST /api/reboot and wait for the measured initial frame")
    parser.add_argument("--select-firmware", metavar="NAME", default=None,
                        help="POST /api/firmware for this file name from the "
                             "bridge's firmware folder before the tour")
    parser.add_argument("--allow-sha-drift", action="store_true",
                        help="record a differing settled frame instead of failing; "
                             "the changed-frame gate stays hard")
    parser.add_argument("--allow-firmware-drift", action="store_true",
                        help="run even when the bridge reports another firmware SHA")
    parser.add_argument("--no-verify-png", action="store_true",
                        help="skip re-hashing the served PNG against the state SHA")
    return parser


def main(argv: list[str] | None = None, parser: argparse.ArgumentParser | None = None) -> int:
    parser = parser or _build_parser()
    args = parser.parse_args(argv)
    try:
        base_url = validate_base_url(args.base_url)
    except ValueError as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2
    if is_loopback_url(base_url) and not args.allow_loopback:
        print("refusing to run: a loopback instance needs --allow-loopback as a "
              "deliberate second choice", file=sys.stderr)
        return 2
    try:
        plan = build_plan(args.sequence)
    except ValueError as exc:
        print(f"invalid tap plan: {exc}", file=sys.stderr)
        return 2
    out = args.out or Path(f"crosspoint-acceptance-{args.sequence}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    transport = HttpTransport(base_url, args.timeout_seconds)
    try:
        report = run_plan(transport, plan, sequence=args.sequence,
                          out_dir=out.parent, step_timeout=args.step_timeout,
                          settle_seconds=args.settle_seconds,
                          settle_polls=args.settle_polls,
                          poll_interval=args.poll_interval,
                          tap_duration_ms=args.tap_duration_ms,
                          reboot_first=args.reboot_first,
                          select_firmware=args.select_firmware,
                          allow_sha_drift=args.allow_sha_drift,
                          allow_firmware_drift=args.allow_firmware_drift,
                          verify_png=not args.no_verify_png)
    except BridgeError as exc:
        report = {"plan": plan, "steps": [], "verdict": {
            "channel": "browser_http", "sequence": args.sequence,
            "base_url": base_url, "navigation_sequence_verified": False,
            "strict_order_ok": False, "aborted": "bridge_unreachable",
            "error": str(exc), "taps_injected": 0, "steps_passed": 0,
            "steps_planned": len(plan),
            "guest_gt911_i2c_consumption_observed": False,
            "full_acceptance": False}}
    out.write_text(json.dumps(report, indent=1) + "\n")
    verdict = report["verdict"]
    print(json.dumps({key: verdict.get(key) for key in (
        "channel", "sequence", "navigation_sequence_verified", "steps_passed",
        "steps_planned", "taps_injected", "first_failing_step", "aborted",
        "strict_order_ok", "reached_screen")}, indent=1))
    if verdict.get("navigation_sequence_verified") is True:
        return 0
    if verdict.get("aborted") == "bridge_unreachable":
        return 3
    return 1


if __name__ == "__main__":
    sys.exit(main())
