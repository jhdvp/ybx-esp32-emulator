"""Multi-step GT911 -> SSD1677 navigation acceptance for the X4 Pro guest.

Host-only pure logic: no QEMU, no sockets, no shared browser server. Every
number below is either a firmware layout fact (cited source line) or a
measurement taken from a genuine guest framebuffer capture; nothing is
inferred from a web UI mock.

Coordinate space
----------------
`probe_x4pro_guest_touch_ui.x4pro_raw_to_portrait()` proves that the X4 Pro
panel swap/flip and the renderer portrait rotation cancel, so a raw GT911
contact (x, y) lands on portrait pixel (x, y) unchanged. The measured portrait
geometry below can therefore be injected as raw GT911 coordinates directly.

Evidence channels
-----------------
The accepted frame SHAs below are genuine guest SSD1677 readouts: the
Home -> More pair comes from the 0042 native QMP run, the Settings pairs from
the 0043 run of the instrumented browser-channel guest (see
experiments/0050-qwen38-telemetry-integration-20261030.md:89-98). They are
measured facts; what has NOT been measured is the native QMP tap driving the
`(210,679)` and `(35,61)` rows/arrows, so the acceptance plan keeps the
guest-I2C consumption gates separate from the image gates and the browser
channel never reports `full_acceptance`.
"""
import struct
import zlib

PORTRAIT_WIDTH = 480
PORTRAIT_HEIGHT = 800
PORTRAIT_STRIDE = PORTRAIT_WIDTH // 8
FRAME_BYTES = PORTRAIT_STRIDE * PORTRAIT_HEIGHT

# Frame buffer is the portrait-packed 1bpp image produced by
# epd_frame_io.ssd1677_landscape_to_portrait(): a cleared bit is black ink.


def is_dark(frame: bytes, x: int, y: int) -> bool:
    if len(frame) != FRAME_BYTES:
        raise ValueError(f"portrait frame must be {FRAME_BYTES} bytes")
    if not (0 <= x < PORTRAIT_WIDTH and 0 <= y < PORTRAIT_HEIGHT):
        raise ValueError("pixel outside the 480x800 portrait panel")
    return not (frame[y * PORTRAIT_STRIDE + (x // 8)] >> (7 - (x % 8))) & 1


def dark_count(frame: bytes, rect: tuple[int, int, int, int]) -> int:
    x0, y0, x1, y1 = rect
    return sum(1 for y in range(y0, y1) for x in range(x0, x1) if is_dark(frame, x, y))


def longest_dark_run(frame: bytes, rect: tuple[int, int, int, int]) -> int:
    """Longest contiguous horizontal ink run in any row of the rect."""
    x0, y0, x1, y1 = rect
    best = 0
    for y in range(y0, y1):
        run = 0
        for x in range(x0, x1):
            run = run + 1 if is_dark(frame, x, y) else 0
            best = max(best, run)
    return best


def ink_bbox(frame: bytes, rect: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    """Tightest (x0, y0, x1, y1) of dark pixels inside rect, or None."""
    x0, y0, x1, y1 = rect
    xs, ys = [], []
    for y in range(y0, y1):
        for x in range(x0, x1):
            if is_dark(frame, x, y):
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs) + 1, max(ys) + 1


def portrait_from_mono_png(path) -> tuple[int, int, bytes]:
    """Return (width, height, portrait-packed 1bpp buffer) stored in a PNG.

    save_mono_png writes the portrait buffer verbatim (one 0x00 filter byte
    per row), so the scanlines are the framebuffer itself: a cleared bit is
    black ink. This is how the browser channel recovers real panel pixels —
    /api/state reports the landscape RAM SHA, but the screen predicate needs
    the portrait image.
    """
    data = open(path, "rb").read() if not isinstance(path, bytes) else path
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    offset, idat, header = 8, b"", None
    while offset < len(data):
        size = struct.unpack(">I", data[offset:offset + 4])[0]
        kind, body = data[offset + 4:offset + 8], data[offset + 8:offset + 8 + size]
        offset += 12 + size
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
    width, height, bit_depth, color_type = header[0], header[1], header[2], header[3]
    if (bit_depth, color_type) != (1, 0):
        raise ValueError(f"expected 1bpp greyscale PNG, got depth={bit_depth} color={color_type}")
    raw = zlib.decompress(idat)
    stride = width // 8
    if len(raw) != height * (stride + 1):
        raise ValueError("PNG scanline data has the wrong length")
    rows = []
    for row in range(height):
        base = row * (stride + 1)
        if raw[base] != 0:
            raise ValueError("PNG row uses a non-zero filter byte")
        rows.append(raw[base + 1:base + 1 + stride])
    return width, height, b"".join(rows)


# Measured from genuine guest frames (dark-pixel counts per 480x800 portrait):
#   home            s3-x4pro-gpio6-pcpu-irq-0042-2s-final-before.png
#   More dialog     s3-x4pro-gpio6-pcpu-irq-0042-2s-final-after.png
#   YBX Settings    s3-batch2-0043-browser-home-more-settings.png
#   System Settings s3-batch2-0043-browser-system-settings.png
# The More dialog is drawn by OptionPopup.h:245-249 (width = 3/4 of the screen,
# centered) and its rounded frame occupies portrait x 56..416, y 239..559.
DIALOG_RECT = (56, 239, 417, 560)
BAND_DIALOG_TOP = (40, 239, 440, 241)
BAND_DIALOG_BOTTOM = (40, 558, 440, 560)
BAND_YBX_SETTINGS_SYSTEM_ROW = (10, 665, 450, 697)
BAND_SYSTEM_SETTINGS_CONNECTIVITY = (65, 505, 175, 536)
# Ybx home header rule (YbxHomeUi.cpp:211 draws it at layout.header.bottom()).
# Measured ink: home 854, More dialog 854, both Settings screens 0 - the
# activities use a different header height, so this band identifies the home
# screen family and nothing else.
BAND_HOME_HEADER = (27, 79, 454, 83)
HOME_HEADER_INK_MIN = 500

# Measured longest contiguous ink run, home / More dialog / YBX Settings /
# System Settings: 12 / 361 / 5 / 0 (top band) and 0 / 354 / 7 / 0 (bottom
# band). A 200 px threshold separates the dialog by >16x from any other frame.
DIALOG_RUN_MIN = 200
# Measured ink in the "System settings" row band: 0 / 0 / 1978 / 0.
YBX_SETTINGS_SYSTEM_ROW_INK_MIN = 1000
# Measured ink in the "Connectivity" row band: 0 / 0 / 61 / 640.
SYSTEM_SETTINGS_CONNECTIVITY_INK_MIN = 300


def more_dialog_visible(frame: bytes) -> bool:
    return (longest_dark_run(frame, BAND_DIALOG_TOP) >= DIALOG_RUN_MIN
            and longest_dark_run(frame, BAND_DIALOG_BOTTOM) >= DIALOG_RUN_MIN)


def ybx_settings_list_visible(frame: bytes) -> bool:
    return dark_count(frame, BAND_YBX_SETTINGS_SYSTEM_ROW) >= YBX_SETTINGS_SYSTEM_ROW_INK_MIN


def system_settings_list_visible(frame: bytes) -> bool:
    return dark_count(frame, BAND_SYSTEM_SETTINGS_CONNECTIVITY) >= SYSTEM_SETTINGS_CONNECTIVITY_INK_MIN


def home_screen_visible(frame: bytes) -> bool:
    return dark_count(frame, BAND_HOME_HEADER) >= HOME_HEADER_INK_MIN


def screen_state(frame: bytes) -> str:
    """Named screen, or "unknown" when no measured panel matches.

    "unknown" is a legitimate verdict for a screen nobody has captured yet -
    including the Wi-Fi screens this guest cannot serve; it must never be
    renamed into a navigation success.
    """
    if home_screen_visible(frame):
        return "more-dialog" if more_dialog_visible(frame) else "home"
    if system_settings_list_visible(frame):
        return "system-settings"
    if ybx_settings_list_visible(frame):
        return "ybx-settings"
    return "unknown"


# Accepted SSD1677 landscape-frame SHA-256 of the genuine guest readouts,
# keyed by screen. Home/More: 0042 native run
# (`s3-x4pro-gpio6-pcpu-irq-0042-2s-final.json`); the two Settings screens and
# the back repaint: 0043 instrumented browser-channel run
# (`s3-batch2-0043-browser-multistep.json`). A live step may legitimately
# repaint differently (pagination, clock, selection), so a mismatch against
# this table is reported as a gate outcome, and only the
# `--require-accepted-shas` flag turns it into a hard failure.
ACCEPTED_SCREEN_SHAS: dict[str, str] = {
    "home": "e79c2ad5f2b60b16fe3f20625742d5f1343f5785552d1b32c11f33c42a874672",
    "more-dialog": "8b81cec46b2dd5690cc2748d80d01a7ee86ca7ffbb2d57c7a92bcb68354222cf",
    "ybx-settings": "5d19930cf416a60c755aec341aaa2749d994998d241def31cbc36a2a9b6b9ba2",
    "system-settings": "a8c871b0ea932bd80018b9d8a2bc1efcbf5562ebe173fff3d7732f5377fe0585",
}


# raw == portrait; see the coordinate-space note above. Coordinates marked
# "measured GT911 tap" were driven against the genuine guest and produced the
# accepted frames; the two corrected in 0062 ((210,679) and (35,61)) replace
# the 0048 report's untested row-centre guesses (240,681) and (66,63).
TARGETS: dict[str, dict] = {
    "home-more": {
        "raw_xy": (350, 760),
        "label": "More tab",
        "evidence": "measured GT911 tap; native run 0042: frame e79c2ad5 -> "
                    "8b81cec4; measured tab pill x246..452 y741..796",
    },
    "more-settings": {
        "raw_xy": (240, 453),
        "label": "Settings option in the More dialog",
        "evidence": "measured GT911 tap; HomeActivity.cpp:714-716 "
                    "OPTIONS[2]=STR_SETTINGS_TITLE; measured label ink bbox "
                    "x195..285 y444..466 in the genuine More frame; drove the "
                    "0043 run that produced frame 5d19930c",
    },
    "settings-system-settings": {
        "raw_xy": (210, 679),
        "label": "System settings row",
        "evidence": "measured GT911 tap; drove 0043 frame 5d19930c -> "
                    "a8c871b0; falls inside the row text bbox x16..444 "
                    "y670..693 measured in the genuine YBX Settings frame",
    },
    "system-settings-back": {
        "raw_xy": (35, 61),
        "label": "Header back arrow",
        "evidence": "measured GT911 tap; drove 0043 frame a8c871b0 -> "
                    "5d19930c; falls inside the header arrow region x24..122 "
                    "y52..74 measured in the genuine System Settings frame",
    },
    "more-dismiss": {
        "raw_xy": (240, 650),
        "label": "Outside the More dialog",
        "evidence": "OptionPopup.h:97-104 dismisses on a release outside the "
                    "dialog rect; measured fully white gap y565..730 x0..480 "
                    "in the genuine More frame",
    },
}

# The full Home -> More -> YBX Settings -> System settings -> Back chain,
# strictly ordered. Every step names its screen predicate (expect_before /
# expect_after, evaluated on real panel pixels) and gates on the accepted
# SSD1677 SHA of the expected screen (`ACCEPTED_SCREEN_SHAS`); the back step
# also replays the ybx-settings frame, so its gate is the same SHA.
SEQUENCES: dict[str, list[dict]] = {
    "home-more-settings-system-settings-back": [
        {"name": "open-more", "target": "home-more",
         "expect_before": "home", "expect_after": "more-dialog"},
        {"name": "open-settings", "target": "more-settings",
         "expect_before": "more-dialog", "expect_after": "ybx-settings"},
        {"name": "open-system-settings", "target": "settings-system-settings",
         "expect_before": "ybx-settings", "expect_after": "system-settings"},
        {"name": "back-to-settings", "target": "system-settings-back",
         "expect_before": "system-settings", "expect_after": "ybx-settings",
         "replay_of": "open-settings"},
    ],
    # Dismissing the dialog repaints home with the More tab highlighted, so
    # the post-dismiss frame is deliberately not compared to the pre-More one.
    "more-and-dismiss": [
        {"name": "open-more", "target": "home-more",
         "expect_before": "home", "expect_after": "more-dialog"},
        {"name": "dismiss-more", "target": "more-dismiss",
         "expect_before": "more-dialog", "expect_after": "home"},
    ],
}

DEFAULT_SEQUENCE = "home-more-settings-system-settings-back"


def resolve_sequence(name: str) -> list[dict]:
    try:
        return [dict(step) for step in SEQUENCES[name]]
    except KeyError:
        raise ValueError(f"unknown sequence {name!r}; choose from "
                         f"{', '.join(sorted(SEQUENCES))}") from None


def ordered_acceptance_plan(name: str = DEFAULT_SEQUENCE, *,
                            require_accepted_shas: bool = False) -> list[dict]:
    """Flatten one sequence into the strictly ordered acceptance plan.

    Each plan item carries the raw GT911 point, the screen predicates and the
    accepted SSD1677 SHA the step must land on. Without
    `require_accepted_shas` the SHA is reported as `expectation_only` and the
    hard gates stay image-change + screen-predicate; with it, any step landing
    on a frame that is not the accepted one fails the run.
    """
    steps = resolve_sequence(name)
    for index, step in enumerate(steps):
        x, y = TARGETS[step["target"]]["raw_xy"]
        step["index"] = index
        step["raw_xy"] = [x, y]
        step["expected_sha256"] = ACCEPTED_SCREEN_SHAS.get(step["expect_after"])
        step["requires_accepted_sha"] = bool(require_accepted_shas)
    for step in steps:
        if step["requires_accepted_sha"] and not step["expected_sha256"]:
            raise ValueError(f"step {step['name']!r} has no accepted SHA for "
                             f"screen {step['expect_after']!r}; cannot require "
                             "accepted SHAs for this sequence")
    return steps


def validate_step_order(plan: list[dict], observed: list[dict]) -> dict:
    """Strict-order check: observed step states must chain through the plan.

    `observed` items carry `screen_before`/`screen_after` exactly as produced
    by `assess_step`. An observed step may not skip ahead, land on a different
    screen than the plan's next predicate, or start from a screen the previous
    accepted step did not reach. Returns ok=False with a machine-readable
    reason; never silently truncates the chain.
    """
    if not plan:
        return {"ok": False, "step": None,
                "reason": "empty_acceptance_plan_for_order_check"}
    if not observed:
        return {"ok": False, "step": None, "reason": "no_observed_steps_for_order_check"}
    for index, item in enumerate(observed):
        if index >= len(plan):
            return {"ok": False, "step": item.get("step"),
                    "reason": "more_steps_observed_than_planned"}
        expected = plan[index]
        if item.get("screen_before") != expected["expect_before"]:
            return {"ok": False, "step": expected["name"],
                    "reason": (f"step_{index}_started_on_"
                               f"{item.get('screen_before')}"),
                    "expected_before": expected["expect_before"]}
        if item.get("screen_after") != expected["expect_after"]:
            return {"ok": False, "step": expected["name"],
                    "reason": (f"step_{index}_reached_"
                               f"{item.get('screen_after')}"),
                    "expected_after": expected["expect_after"]}
    return {"ok": True, "steps_checked": len(observed)}


def _delta(after: dict, before: dict, key: str) -> int:
    return int(after.get(key, 0)) - int(before.get(key, 0))


def assess_step(step: dict, before: dict, after: dict, touch_before: dict,
                touch_after: dict, injections: dict) -> dict:
    """One tap is accepted only on guest-consumed input plus a real panel change.

    QMP accepting a property write proves nothing: the guest must read the
    point over I2C and clear the contact status, and the SSD1677 image must
    reach the screen named in the step. A step that does not change the panel
    image fails loudly (`panel_pixels_unchanged`), even when the plan SHA gate
    is switched off. `touch_before`/`touch_after` may be empty for channels
    that cannot observe the guest's GT911 I2C consumption (the browser
    instance); that is recorded as `full_acceptance: false`, never faked.
    """
    reasons: list[str] = []
    native_counters = bool(touch_before or touch_after)
    if not all(item.get("ok", item.get("accepted", False))
               for item in injections.values()):
        reasons.append("qmp_injection_rejected")
    if native_counters:
        if _delta(touch_after, touch_before, "point-reads") < 1:
            reasons.append("guest_did_not_read_gt911_point")
        if _delta(touch_after, touch_before, "status-clears") < 2:
            reasons.append("guest_did_not_clear_press_and_release")
    if int(after.get("frames", 0)) <= int(before.get("frames", 0)):
        reasons.append("epd_refresh_count_not_increased")
    before_sha, after_sha = before.get("frame_sha256"), after.get("frame_sha256")
    if not before_sha or not after_sha:
        reasons.append("missing_visible_frame_readout")
    elif before_sha == after_sha:
        reasons.append("panel_pixels_unchanged")
    if before.get("ready") is False:
        reasons.append("guest_not_ready")
    state_before, state_after = before.get("screen_state"), after.get("screen_state")
    if state_before != step["expect_before"]:
        reasons.append(f"precondition_screen_is_{state_before}")
    if state_after != step["expect_after"]:
        reasons.append(f"target_screen_is_{state_after}")
    expected_sha = step.get("expected_sha256")
    if step.get("requires_accepted_sha") and expected_sha and after_sha != expected_sha:
        reasons.append("accepted_sha256_mismatch")
    if expected_sha and after_sha:
        sha_outcome = "matched" if after_sha == expected_sha else "mismatched"
    elif expected_sha:
        sha_outcome = "missing_readout"
    else:
        sha_outcome = "expectation_only"
    return {
        "step": step["name"],
        "target": step["target"],
        "raw_xy": list(TARGETS[step["target"]]["raw_xy"]),
        "expected_screen_before": step["expect_before"],
        "expected_screen_after": step["expect_after"],
        "screen_before": state_before,
        "screen_after": state_after,
        "frame_sha256_before": before_sha,
        "frame_sha256_after": after_sha,
        "expected_frame_sha256": expected_sha,
        "accepted_sha_gate": sha_outcome,
        "requires_accepted_sha": bool(step.get("requires_accepted_sha")),
        "png_before": before.get("png"),
        "png_after": after.get("png"),
        "epd_frames_before": before.get("frames"),
        "epd_frames_after": after.get("frames"),
        "gt911_point_reads": _delta(touch_after, touch_before, "point-reads"),
        "gt911_status_clears": _delta(touch_after, touch_before, "status-clears"),
        "guest_gt911_i2c_consumption_observed": native_counters,
        "full_acceptance": bool(native_counters),
        "passed": not reasons,
        "failure_codes": reasons,
    }


def assess_sequence(step_results: list[dict], plan: list[dict] | None = None) -> dict:
    """A sequence is verified only when every step is verified, in order."""
    failed = [r for r in step_results if not r["passed"]]
    order_check = validate_step_order(plan, step_results) if plan is not None else None
    return {
        "steps_run": len(step_results),
        "steps_passed": len(step_results) - len(failed),
        "navigation_sequence_verified": bool(step_results) and not failed,
        "first_failing_step": None if not failed else failed[0]["step"],
        "reached_screen": step_results[-1]["screen_after"] if step_results else None,
        "frame_sha256_chain": [r["frame_sha256_after"] for r in step_results],
        "strict_order_ok": None if order_check is None else order_check["ok"],
        "order_check": order_check,
    }


def failure_diagnostics(step_results: list[dict]) -> list[dict]:
    """Machine-readable first cause per failing step, in execution order."""
    return [{"step": r["step"], "target": r["target"], "codes": r["failure_codes"],
             "screen_expected": r["expected_screen_after"], "screen_observed": r["screen_after"]}
            for r in step_results if not r["passed"]]


def panel_replay_signal(step_results: list[dict], replay_of: str, step_name: str) -> dict:
    """Report whether a back step repaints the byte-identical earlier frame.

    The genuine 0043 capture shows this holds (back from System Settings
    reproduced sha 5d19930c), but a legitimate repaint may differ, so this is
    recorded evidence and never a gate.
    """
    origin = next((r for r in step_results if r["step"] == replay_of), None)
    target = next((r for r in step_results if r["step"] == step_name), None)
    if not origin or not target or not origin["frame_sha256_after"]:
        return {"available": False, "reason": "reference frame not captured"}
    return {"available": True,
            "reference_step": replay_of,
            "reference_sha256": origin["frame_sha256_after"],
            "returned_sha256": target["frame_sha256_after"],
            "restores_previous_frame": (target["frame_sha256_after"]
                                        == origin["frame_sha256_after"])}
