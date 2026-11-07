"""Deterministic UI anchors from the genuine offline Stock X4 Pro QEMU guest.

These crops are *not* the UI implementation. They recognise pixels emitted by
real firmware, and fail closed if the expected widget is not on the e-paper.
The sample test EPUB and firmware version are deliberately pinned.
"""
from __future__ import annotations

import struct
import zlib
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
}

# Stock's More rows move by exactly one 72px slot when View Clippings is
# inserted beneath Create Clipping. Both variants were captured from the
# original 2.0 RC QEMU guest; neither is a guessed dark-pixel heuristic.
ALTERNATE_ANCHORS = {
    "created": ((24, 588),),
}


def _luma_pixels(data: bytes) -> tuple[int, int, bytes]:
    """Decode an 8-bit greyscale or RGB PNG into per-pixel luma bytes.

    Pillow stays a live-run-only dependency, so the host suite reads PNGs with
    the standard library. The luma coefficients mirror Pillow's RGB->L
    conversion, so anchor scores are identical either way.
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    offset, idat, header = 8, b"", None
    while offset + 12 <= len(data):
        size = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + size]
        offset += 12 + size
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if header is None:
        raise ValueError("PNG has no IHDR chunk")
    width, height, depth, color = header[0], header[1], header[2], header[3]
    if depth != 8 or color not in (0, 2):
        raise ValueError(f"expected 8-bit greyscale or RGB PNG, got depth={depth} color={color}")
    channels = 1 if color == 0 else 3
    stride = width * channels
    raw = zlib.decompress(idat)
    if len(raw) != height * (stride + 1):
        raise ValueError("PNG scanline data has the wrong length")
    previous = bytearray(stride)
    pixels = bytearray()
    pos = 0
    for _ in range(height):
        kind = raw[pos]
        row = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += stride + 1
        if kind not in range(5):
            raise ValueError("unknown PNG filter")
        for i in range(stride):
            a = row[i - channels] if i >= channels else 0
            b = previous[i]
            c = previous[i - channels] if i >= channels else 0
            if kind == 1:
                predictor = a
            elif kind == 2:
                predictor = b
            elif kind == 3:
                predictor = (a + b) // 2
            elif kind == 4:
                p = a + b - c
                delta = (abs(p - a), abs(p - b), abs(p - c))
                predictor = (a, b, c)[delta.index(min(delta))]
            else:
                predictor = 0
            row[i] = (row[i] + predictor) & 255
        if channels == 1:
            pixels.extend(row)
        else:
            for i in range(0, stride, 3):
                pixels.append((row[i] * 299 + row[i + 1] * 587 + row[i + 2] * 114) // 1000)
        previous = row
    return width, height, bytes(pixels)


def _crop(picture: bytes, width: int, height: int,
          x: int, y: int, crop_width: int, crop_height: int) -> bytes:
    """Cut a rectangle out of the framebuffer; pixels outside stay black."""
    out = bytearray(crop_width * crop_height)
    for dy in range(crop_height):
        sy = y + dy
        if not 0 <= sy < height:
            continue
        base = sy * width
        for dx in range(crop_width):
            sx = x + dx
            if 0 <= sx < width:
                out[dy * crop_width + dx] = picture[base + sx]
    return bytes(out)


def matches_guest_screen(data: bytes, name: str, *, threshold: float = 0.77) -> bool:
    """Compare black glyphs, not a scalar darkness heuristic.

    Uses small, static, copyright-free screen fragments from a local synthetic
    book. The matcher never calls OCR, observes DOM, or queries a model mock.
    """
    if name not in ANCHORS:
        raise ValueError(f"unknown guest screen anchor: {name}")
    width, height, picture = _luma_pixels(data)
    marker_width, marker_height, marker = _luma_pixels(
        (TEMPLATES / f"{name}.png").read_bytes())
    ideal = [v < 115 for v in marker]
    total = sum(ideal)
    if total < 12:
        raise ValueError(f"screen anchor is too sparse: {name}")
    for x, y in (ANCHORS[name], *ALTERNATE_ANCHORS.get(name, ())):
        for dy in (-2, -1, 0, 1, 2):
            for dx in (-2, -1, 0, 1, 2):
                part = _crop(picture, width, height, x + dx, y + dy,
                             marker_width, marker_height)
                actual = [v < 115 for v in part]
                matched = sum(a and b for a, b in zip(actual, ideal))
                present = sum(actual)
                # Precision and recall: blank/irrelevant locations must NOT pass.
                score = 2 * matched / (present + total) if present + total else 0
                if score >= threshold:
                    return True
    return False
