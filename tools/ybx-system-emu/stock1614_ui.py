"""Deterministic UI anchors from the genuine offline Stock X4 Pro QEMU guest.

These crops are *not* the UI implementation. They recognise pixels emitted by
real firmware, and fail closed if the expected widget is not on the e-paper.
The sample test EPUB and firmware version are deliberately pinned.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent / "stock1614_ui_templates"
# The template crop and its on-screen location share an origin; tolerate
# refresh jitter/one-pixel glyph shifts, but not a different menu or title.
ANCHORS = {
    "home": (50, 142),
    "home_recent": (181, 162),
    "sd_root": (54, 48),
    "fixture_folder": (54, 48),
    "books_folder": (53, 47),
    "file_dialog": (175, 312),
    "reader": (18, 20),
    "more": (19, 318),
    "created": (24, 516),
    "sync": (23, 582),
    "wifi": (32, 174),
    "clip_action": (234, 148),
    "wifi_connecting": (158, 349),
    "sync_failed": (182, 356),
    # Live pairing-pause gates, captured from the genuine 2.0 RC X4 Pro guest on
    # this branch. Both are static firmware *text* crops: the Plugins header word
    # and the "Waiting for authorization…" line. Neither contains a QR, device
    # code or pairing link, so they are safe to commit while still failing closed
    # on any other screen.
    "plugins": (58, 54),
    "pairing_waiting": (78, 371),
}

# Stock's More rows move by exactly one 72px slot when View Clippings is
# inserted beneath Create Clipping. Both variants were captured from the
# original 2.0 RC QEMU guest; neither is a guessed dark-pixel heuristic.
ALTERNATE_ANCHORS = {
    "created": ((24, 588),),
}


def matches_guest_screen(data: bytes, name: str, *, threshold: float = 0.77) -> bool:
    """Compare black glyphs, not a scalar darkness heuristic.

    Uses small, static, copyright-free screen fragments from a local synthetic
    book. The matcher never calls OCR, observes DOM, or queries a model mock.
    """
    from PIL import Image

    if name not in ANCHORS:
        raise ValueError(f"unknown guest screen anchor: {name}")
    picture = Image.open(BytesIO(data)).convert("L")
    marker = Image.open(TEMPLATES / f"{name}.png").convert("L")
    ideal = [v < 115 for v in (marker.get_flattened_data() if hasattr(marker, "get_flattened_data") else marker.getdata())]
    total = sum(ideal)
    if total < 12:
        raise ValueError(f"screen anchor is too sparse: {name}")
    for x, y in (ANCHORS[name], *ALTERNATE_ANCHORS.get(name, ())):
        for dy in (-2, -1, 0, 1, 2):
            for dx in (-2, -1, 0, 1, 2):
                part = picture.crop((x+dx, y+dy, x+dx+marker.width,
                                     y+dy+marker.height))
                actual = [v < 115 for v in (part.get_flattened_data() if hasattr(part, "get_flattened_data") else part.getdata())]
                matched = sum(a and b for a, b in zip(actual, ideal))
                present = sum(actual)
                # Precision and recall: blank/irrelevant locations must NOT pass.
                score = 2 * matched / (present + total) if present + total else 0
                if score >= threshold:
                    return True
    return False
