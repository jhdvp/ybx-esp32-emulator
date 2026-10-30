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
"""

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
# YBX home header rule (YbxHomeUi.cpp:211 draws it at layout.header.bottom()).
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


# raw == portrait; see the coordinate-space note above.
TARGETS: dict[str, dict] = {
    "home-more": {
        "raw_xy": (350, 760),
        "label": "More tab",
        "evidence": "native guest run 0042: frame e79c2ad5 -> 8b81cec4; "
                    "measured tab pill x246..452 y741..796",
    },
    "more-settings": {
        "raw_xy": (240, 453),
        "label": "Settings option in the More dialog",
        "evidence": "HomeActivity.cpp:714-716 OPTIONS[2]=STR_SETTINGS_TITLE; "
                    "measured label ink bbox x195..285 y444..466 in the genuine "
                    "More frame; same point drove the 0043 browser capture",
    },
    "settings-system-settings": {
        "raw_xy": (240, 681),
        "label": "System settings row",
        "evidence": "measured row ink bbox x16..443 y670..692 in the genuine "
                    "YBX Settings frame (sha 5d19930c)",
    },
    "system-settings-back": {
        "raw_xy": (66, 63),
        "label": "Header back arrow",
        "evidence": "measured arrow ink bbox x24..108 y54..73 in the genuine "
                    "System Settings frame (sha a8c871b0)",
    },
    "more-dismiss": {
        "raw_xy": (240, 650),
        "label": "Outside the More dialog",
        "evidence": "OptionPopup.h:97-104 dismisses on a release outside the "
                    "dialog rect; measured fully white gap y565..730 x0..480 "
                    "in the genuine More frame",
    },
}

SEQUENCES: dict[str, list[dict]] = {
    "more-to-settings-and-back": [
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


def resolve_sequence(name: str) -> list[dict]:
    try:
        return [dict(step) for step in SEQUENCES[name]]
    except KeyError:
        raise ValueError(f"unknown sequence {name!r}; choose from "
                         f"{', '.join(sorted(SEQUENCES))}") from None


def _delta(after: dict, before: dict, key: str) -> int:
    return int(after.get(key, 0)) - int(before.get(key, 0))


def assess_step(step: dict, before: dict, after: dict, touch_before: dict,
                touch_after: dict, injections: dict) -> dict:
    """One tap is accepted only on guest-consumed input plus a real panel change.

    QMP accepting a property write proves nothing: the guest must read the
    point over I2C and clear the contact status, and the SSD1677 image must
    reach the screen named in the step.
    """
    reasons: list[str] = []
    if not all(item.get("ok") for item in injections.values()):
        reasons.append("qmp_injection_rejected")
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
    state_before, state_after = before.get("screen_state"), after.get("screen_state")
    if state_before != step["expect_before"]:
        reasons.append(f"precondition_screen_is_{state_before}")
    if state_after != step["expect_after"]:
        reasons.append(f"target_screen_is_{state_after}")
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
        "png_before": before.get("png"),
        "png_after": after.get("png"),
        "epd_frames_before": before.get("frames"),
        "epd_frames_after": after.get("frames"),
        "gt911_point_reads": _delta(touch_after, touch_before, "point-reads"),
        "gt911_status_clears": _delta(touch_after, touch_before, "status-clears"),
        "passed": not reasons,
        "failure_codes": reasons,
    }


def assess_sequence(step_results: list[dict]) -> dict:
    """A sequence is verified only when every step is verified, in order."""
    failed = [r for r in step_results if not r["passed"]]
    return {
        "steps_run": len(step_results),
        "steps_passed": len(step_results) - len(failed),
        "navigation_sequence_verified": bool(step_results) and not failed,
        "first_failing_step": None if not failed else failed[0]["step"],
        "reached_screen": step_results[-1]["screen_after"] if step_results else None,
        "frame_sha256_chain": [r["frame_sha256_after"] for r in step_results],
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
