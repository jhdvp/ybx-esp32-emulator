#!/usr/bin/env python3
"""What does firmware for the OTHER boards do on this single X4 Pro machine?

CrossPoint publishes one application image per board (x3-x4, x4c, x4pro,
sticky, picco, metalio_eink4, papermono, eego_a4) and inkMOD publishes x4pro,
x4classic and x3x4. The emulator models exactly one machine: an ESP32-S3 with
an SSD1677 480x800 portrait panel (800x480 native landscape) on the pins the
X4 Pro uses. This tool measures what happens when the other boards' images are
put on it, instead of guessing.

Pipeline, one file, several roles:

* ``fetch``    - read-only ``gh release download`` of every board asset of a
  release into a scratch folder;
* ``classify`` - decide from the BYTES what each file is (ESP flash image /
  app-only image / FAT-SD image / unknown) and read its ESP image header;
* ``merge``    - place an app-only image at 0x10000 under the X4 Pro base
  (bootloader at 0x0, partition table at 0x8000) padded to 16 MiB. That is the
  rule firmware/README.txt describes, and this module reproduces the three
  merged images already in that folder byte-for-byte (see the host tests);
* ``boot``     - start each merged image in its OWN disposable container
  (boot_matrix.boot_one_via_container) and record painted / painted nothing /
  QEMU died / refused plus the first-frame SHA-256;
* ``run``      - all of the above.

Host tests (test_panel_matrix.py) exercise classify / merge / verdict /
expectation logic only: no network, no Docker, no guest.

Exit codes: 0 when every row carries a real verdict, 1 when the scratch folder
or the base image is unusable, 2 when at least one row ended in a runner error
(an unknown, not a guest statement).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

# --- flash layout (the rule firmware/README.txt states, verified 2026-10-31)
BOOTLOADER_OFFSET = 0x0000
PARTITION_OFFSET = 0x8000
APP_OFFSET = 0x10000
FLASH_SIZE = 16 * 1024 * 1024
ERASED_BYTE = 0xFF
IMAGE_MAGIC = 0xE9
PARTITION_MAGIC = b"\xaa\x50"
MBR_SIGNATURE = b"\x55\xaa"

# The X4 Pro base = the 64 KiB head (bootloader + partition table) of
# firmware/crosspoint-2.0.0rc-x4pro-flash-16m.bin. Identical in the 1.6.5 and
# inkMOD merged images there; pinned by SHA-256 so a silent base change cannot
# pass unnoticed.
DEFAULT_BASE_IMAGE = Path(
    "/Users/yaraslauhancharou/Projects/YBX/artifacts/"
    "ybx-esp32-system-emulation-20261029/firmware/"
    "crosspoint-2.0.0rc-x4pro-flash-16m.bin")
X4PRO_BASE_HEAD_SHA256 = (
    "7c89d75bdb8858a4002c603654287c1b589f62af89dc52159c878950a89d3c39")

# ESP-IDF image header chip_id values, only the ones this matrix observes plus
# the documented family prefixes; anything else stays None (never guessed).
CHIP_IDS = {0x0000: "esp32", 0x0002: "esp32s2", 0x0005: "esp32c3",
            0x0009: "esp32s3"}

# --- upstream releases ----------------------------------------------------
CROSSPOINT_REPO = "crosspoint-reader/crosspoint-reader"
INKMOD_REPO = "alpzoloto-sudo/inkmod"
CROSSPOINT_BOARDS = ("x3-x4", "x4c", "x4pro", "sticky", "picco",
                     "metalio_eink4", "papermono", "eego_a4")
INKMOD_BOARDS = ("x4pro", "x4classic", "x3x4")
DEFAULT_RELEASES = (
    {"vendor": "crosspoint", "repo": CROSSPOINT_REPO, "tag": "2.0.0rc",
     "boards": CROSSPOINT_BOARDS},
    # 1.6.5 published five boards only (gh api .../releases: papermono,
    # sticky, x3-x4, x4c, x4pro); picco, metalio_eink4 and eego_a4 came with
    # 2.0.0rc.
    {"vendor": "crosspoint", "repo": CROSSPOINT_REPO, "tag": "1.6.5",
     "boards": ("x3-x4", "x4c", "x4pro", "sticky", "papermono")},
    {"vendor": "inkmod", "repo": INKMOD_REPO, "tag": "v1.1.10",
     "boards": INKMOD_BOARDS},
)

# --- outcomes -------------------------------------------------------------
PAINTED = "painted"
PAINTED_NOTHING = "painted nothing"
DIED = "QEMU died"
REFUSED = "refused"
RUNNER_ERROR = "runner error"
OUTCOMES = (PAINTED, PAINTED_NOTHING, DIED, REFUSED, RUNNER_ERROR)

# --- machine the emulator actually models ---------------------------------
X4PRO_MACHINE = {
    "chip": "esp32s3",
    "qemu_machine": ("esp32s3,ybx-x4pro-i2c=on,ybx-x4pro-gt911=on,"
                     "ybx-x4pro-sd-power=on,ybx-x4pro-spi2=on,"
                     "ybx-x4pro-epd=on"),
    "epd_controller": "SSD1677",
    "panel": "480x800 portrait (800x480 native landscape), 1 bpp",
    "epd_pins": {"sclk": 12, "mosi": 11, "cs": 13, "dc": 18, "rst": 14,
                 "busy": 6},
    "touch": "GT911 on I2C (SDA39/SCL38/INT10/RST4)",
}


class MergeError(ValueError):
    """The bytes in front of us cannot become an X4 Pro flash image."""


# --- classification -------------------------------------------------------
def classify_bytes(data):
    """What IS this file, judged by its own bytes only.

    * ``flash``   - bootloader magic 0xE9 at 0x0, partition table 0xAA50 at
      0x8000, app magic 0xE9 at 0x10000: bootable as-is (the bridge rule,
      serve_x4pro_browser.py).
    * ``app``     - a valid ESP image at 0x0 but NO partition table at 0x8000.
      Every published CrossPoint and inkMOD board asset is this, so the bridge
      refuses all of them; they need the X4 Pro base merged under them.
    * ``sd``      - MBR signature 0x55AA at 0x1FE.
    * ``unknown`` - anything else, including truncated files.
    """
    if len(data) >= 0x200 and data[0x1FE:0x200] == MBR_SIGNATURE \
            and data[0] != IMAGE_MAGIC:
        return "sd"
    if len(data) < 0x200 or data[0] != IMAGE_MAGIC:
        return "unknown"
    if (len(data) >= APP_OFFSET + 1
            and data[PARTITION_OFFSET:PARTITION_OFFSET + 2] == PARTITION_MAGIC
            and data[APP_OFFSET] == IMAGE_MAGIC):
        return "flash"
    return "app"


def classify_file(path):
    """(kind, bytes) for a readable file; ("unknown", None) when unreadable."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return "unknown", None
    return classify_bytes(data), data


def esp_header(data):
    """Parse the ESP image header, or None when these are not ESP image bytes.

    magic(1) segment_count(1) flash_mode(1) flash_size_freq(1) entry(4) wp(1)
    spi_pin(3) chip_id(2) min_revision(2) - chip_id sits at 0xC in the
    extended header ESP-IDF writes since 4.5 (verified against the published
    board assets: x3-x4 reports 0x0005 = ESP32-C3, every other board 0x0009 =
    ESP32-S3).
    """
    if len(data) < 0x12 or data[0] != IMAGE_MAGIC:
        return None
    entry = int.from_bytes(data[4:8], "little")
    chip_id = int.from_bytes(data[0xC:0xE], "little")
    return {
        "segment_count": data[1],
        "flash_mode": data[2],
        "flash_size_freq": data[3],
        "entry_addr": entry,
        "chip_id": chip_id,
        "chip": CHIP_IDS.get(chip_id),
        "min_revision": int.from_bytes(data[0xE:0x10], "little"),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def board_from_asset_name(name):
    """Board slug + vendor inferred from the published asset filename.

    CrossPoint: ``crosspoint-<tag>-<board>.bin``; inkMOD: ``firmware-<board>-
    <tag>.bin``. Returns None when the name carries no board slug, so the
    caller can report "unattributed" instead of inventing a board.
    """
    text = Path(name).name
    match = re.match(r"crosspoint-[^-]+(?:rc)?-(?P<board>[a-z0-9_\-]+)\.bin$",
                     text, re.IGNORECASE)
    if match:
        return {"vendor": "crosspoint", "board": match.group("board")}
    match = re.match(r"firmware-(?P<board>[a-z0-9_\-]+?)-(?:v[\d.]+|release)"
                     r"(?:-v[\d.]+)?\.bin$", text, re.IGNORECASE)
    if match:
        return {"vendor": "inkmod", "board": match.group("board")}
    return None


def asset_name(vendor, tag, board):
    """The published filename for one board of one release."""
    if vendor == "crosspoint":
        return f"crosspoint-{tag}-{board}.bin"
    return f"firmware-{board}-{tag}.bin"


# --- merge ----------------------------------------------------------------
def base_head(path=DEFAULT_BASE_IMAGE):
    """The 64 KiB X4 Pro bootloader+partitions head, verified by SHA-256.

    Raises MergeError with the real reason (missing file, wrong size, wrong
    partition magic, different bytes than pinned) - the merge never proceeds
    on a base it cannot name.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MergeError(f"base image unreadable: {exc}") from exc
    if len(data) < APP_OFFSET:
        raise MergeError(f"base image shorter than 0x{APP_OFFSET:x}: "
                         f"{len(data)} bytes")
    if data[PARTITION_OFFSET:PARTITION_OFFSET + 2] != PARTITION_MAGIC:
        raise MergeError("base image has no partition table at 0x8000")
    head = data[BOOTLOADER_OFFSET:APP_OFFSET]
    digest = hashlib.sha256(head).hexdigest()
    if digest != X4PRO_BASE_HEAD_SHA256:
        raise MergeError(f"base head SHA-256 is {digest}, expected "
                         f"{X4PRO_BASE_HEAD_SHA256}")
    if head[BOOTLOADER_OFFSET] != IMAGE_MAGIC:
        raise MergeError("base image has no bootloader magic at 0x0")
    return head


def build_flash_image(head, app, *, app_offset=APP_OFFSET,
                      flash_size=FLASH_SIZE):
    """Merge an app-only image under an X4 Pro head; return 16 MiB of bytes.

    Same rule the firmware folder uses: bootloader at 0x0, partition table at
    0x8000, application at 0x10000, the rest 0xFF (erased flash). Everything
    that makes this impossible is a MergeError, never a silent truncation.
    """
    if len(head) != app_offset:
        raise MergeError(f"head is {len(head)} bytes, must land exactly at "
                         f"0x{app_offset:x}")
    if head[PARTITION_OFFSET:PARTITION_OFFSET + 2] != PARTITION_MAGIC \
            and app_offset > PARTITION_OFFSET:
        raise MergeError("head carries no partition table magic at 0x8000")
    if len(app) < 0x200 or app[0] != IMAGE_MAGIC:
        raise MergeError("app image is not an ESP image (no 0xE9 magic)")
    if classify_bytes(app) == "flash":
        raise MergeError("app image already is a flash image; nothing to merge")
    if app_offset + len(app) > flash_size:
        raise MergeError(f"app of {len(app)} bytes does not fit at 0x{app_offset:x} "
                         f"in a {flash_size}-byte flash")
    return head + app + bytes([ERASED_BYTE]) * (flash_size - app_offset - len(app))


def merged_name(board, vendor, tag):
    return f"{vendor}-{tag}-{board}-x4pro-flash-16m.bin"


MERGED_SUFFIX = "-x4pro-flash-16m.bin"


def board_from_merged_name(name):
    """Invert merged_name: {vendor, tag, board}, or None for a foreign file.

    The vendor and tag parts carry no dash, so splitting twice leaves a board
    slug that may contain one (``x3-x4``).
    """
    text = Path(name).name
    if not text.endswith(MERGED_SUFFIX):
        return None
    parts = text[: -len(MERGED_SUFFIX)].split("-", 2)
    if len(parts) != 3:
        return None
    vendor, tag, board = parts
    return {"vendor": vendor, "tag": tag, "board": board}


def merge_asset(app_path, out_path, *, head=None, base_image=DEFAULT_BASE_IMAGE):
    """Merge one downloaded asset into a 16 MiB image; returns the result row."""
    app_path, out_path = Path(app_path), Path(out_path)
    row = {"source": str(app_path), "output": str(out_path)}
    try:
        data = app_path.read_bytes()
    except OSError as exc:
        row.update(ok=False, error=f"source unreadable: {exc}")
        return row
    kind = classify_bytes(data)
    row["source_kind"] = kind
    if kind == "flash":
        # Already a complete image: copying is honest, merging is not needed.
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data[:FLASH_SIZE] if len(data) >= FLASH_SIZE
                             else data + bytes([ERASED_BYTE]) * (FLASH_SIZE - len(data)))
        row.update(ok=True, action="copied")
        return row
    if kind != "app":
        row.update(ok=False, error=f"not an ESP application image (kind={kind})")
        return row
    try:
        head_bytes = head if head is not None else base_head(base_image)
        image = build_flash_image(head_bytes, data)
    except MergeError as exc:
        row.update(ok=False, error=str(exc))
        return row
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(image)
    row.update(ok=True, action="merged", app_offset=APP_OFFSET,
               flash_size=FLASH_SIZE, merged_sha256=hashlib.sha256(image).hexdigest())
    return row


# --- verdict --------------------------------------------------------------
def classify_outcome(kind, frames, exit_status, error=None, merge_error=None):
    """Map one observation onto an honest verdict label.

    ``kind`` is the byte-level classification of the SOURCE asset,
    ``merge_error`` says the merge never produced an image, ``frames`` the EPD
    frame counter last read from QOM (None when QEMU never answered),
    ``exit_status`` the QEMU wait status (None when the runner stopped a live
    guest deliberately) and ``error`` a runner fault - explicitly NOT a
    statement about the guest.
    """
    if error:
        return RUNNER_ERROR
    if merge_error or kind in ("unknown", "sd"):
        return REFUSED
    if isinstance(frames, int) and frames > 0:
        return PAINTED
    if frames is None and exit_status is None:
        return RUNNER_ERROR
    return PAINTED_NOTHING if exit_status is None else DIED


def row_from_observation(name, board, vendor, source, observed, merge_row,
                         header):
    """The JSON row for one board: bytes, merge, boot observation, verdict."""
    kind = merge_row.get("source_kind", "unknown")
    merge_error = None if merge_row.get("ok") else merge_row.get("error")
    outcome = classify_outcome(kind, observed.get("frames"),
                               observed.get("qemu_exit_status"),
                               observed.get("error"), merge_error)
    painted = outcome == PAINTED
    return {
        "name": name,
        "vendor": vendor,
        "board": board,
        "chip_from_header": (header or {}).get("chip"),
        # Decided by the bytes, before any guest runs: the machine is an
        # ESP32-S3, so an image built for another chip cannot execute here.
        "chip_matches_machine": ((header or {}).get("chip")
                                 == X4PRO_MACHINE["chip"]) if header else None,
        "entry_addr": (f"{(header or {}).get('entry_addr'):#010x}"
                       if header else None),
        "source_path": str(source),
        "source_kind": kind,
        "source_sha256": (header or {}).get("sha256"),
        "size_bytes": (header or {}).get("size_bytes"),
        "merged_path": merge_row.get("output"),
        "merged_sha256": merge_row.get("merged_sha256"),
        "merge_error": merge_error,
        "verdict": outcome,
        "frames": observed.get("frames") if outcome not in (REFUSED,) else None,
        "first_frame_sha256": (observed.get("first_frame_sha256")
                               if painted else None),
        "first_frame_seconds": (observed.get("first_frame_seconds")
                                 if painted else None),
        "last_frame_sha256": (observed.get("last_frame_sha256")
                              if painted else None),
        "wall_seconds": observed.get("wall_seconds"),
        "qemu_exit_status": observed.get("qemu_exit_status"),
        "qemu_log_tail": observed.get("qemu_log_tail"),
        "error": observed.get("error"),
        "expectation": expectation_for(board),
    }


def summarise(rows):
    counts = {outcome: 0 for outcome in OUTCOMES}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    counts["rows"] = len(rows)
    return counts


# --- what each board actually expects -------------------------------------
# Facts read from the CrossPoint sources (freeink-sdk BoardConfig.h profiles
# and the FreeInkDisplay drivers) and platformio.ini; the file:line references
# are quoted so a reader can re-check them. "machine_gap" names the emulator
# property that a board would need before it could paint here.
BOARD_EXPECTATIONS = {
    "x4pro": {
        "chip": "esp32s3", "controller": "SSD1677 boot default, the UC8179/"
            "UC8279 variants are chosen by the display-bus probe / NVS "
            "screenType",
        "resolution": "800x480 native",
        "pins": "EPD SCLK12/MOSI11/CS13/DC18/RST14/BUSY6 @10 MHz; "
            "GT911 SDA39/SCL38/INT10/RST4 (rail GPIO2 active-low)",
        "evidence": "BoardConfig.h:1791-1795,1803,1840-1846; "
            "FreeInkDisplay.cpp:198; QEMU wiring esp32s3.c:1008-1022",
        "machine_gap": "none - this is the machine the emulator models",
    },
    "x4c": {
        "chip": "esp32s3", "controller": "SSD1677 default + UC8179/UC8279 probe",
        "resolution": "800x480 native",
        "pins": "EPD SCLK12/MOSI11/CS13/DC14/RST10/BUSY18; no touch",
        "evidence": "BoardConfig.h:1944-1950,1977,1981",
        "machine_gap": "EPD DC/RST/BUSY pin map differs from X4 Pro, and the "
            "GT911 must be off; needs per-machine EPD pin properties",
    },
    "x3-x4": {
        "chip": "esp32c3", "controller": "X4 SSD1677 800x480 / X3 UC8253 "
            "(newer batch UC8279d) 792x528",
        "resolution": "800x480 (X4) or 792x528 (X3)",
        "pins": "EPD SCLK8/MOSI10/CS21/DC4/RST5/BUSY6",
        "evidence": "BoardConfig.h:950-953,988-991,1027-1029; "
            "platformio.ini:11,310-311 (esp32-c3-devkitm-1)",
        "machine_gap": "different CPU family: the image is RISC-V ESP32-C3 "
            "(chip_id 0x0005), so an xtensa-only QEMU cannot execute it at "
            "all; a second machine (esp32c3) plus a UC8253 device would be "
            "needed",
    },
    "sticky": {
        "chip": "esp32s3", "controller": "SSD1677", "resolution": "800x480 native",
        "pins": "EPD SCLK13/MOSI14/CS15/DC16/RST17/BUSY18, EPD_EN 47; "
            "GT911 SDA3/SCL2/INT21/RST41 (rail 42)",
        "evidence": "BoardConfig.h:1549-1552,1575,1579; platformio.ini:358",
        "machine_gap": "every EPD pin and the touch I2C bus differ; also no "
            "BOARD_HAS_PSRAM flag, so the PSRAM model must stay off",
    },
    "picco": {
        "chip": "esp32s3", "controller": "SSD1677 or the Onyx Picco "
            "SE0400NQW47 (stock name \"SSD2677\", UltraChip-style command set, "
            "CMD 0x70 -> 07 01 01), chosen by piccoProbePanel()",
        "resolution": "800x480 native; the SSD2677 path sends 2 bits per pixel "
            "per frame",
        "pins": "EPD SCLK48/MOSI47/CS18/DC17/RST16/BUSY15 @20 MHz; "
            "PiccoCst touch @0x24 SDA7/SCL8/INT11/RST10",
        "evidence": "BoardConfig.h:1718-1720,1725,1749; Ssd2677Driver.h:3-16; "
            "FreeInkDisplay.cpp:214-221; platformio.ini:699,705 "
            "(partitions_picco.csv: app ota_0 at 0xE0000)",
        "machine_gap": "EPD pin map, 20 MHz SPI, an UltraChip command set with "
            "active-low BUSY and a 2bpp plane format, plus a different app "
            "partition offset in its own layout (0xE0000, not 0x10000)",
    },
    "metalio_eink4": {
        "chip": "esp32s3", "controller": "SSD1677 with its own configuration",
        "resolution": "800x480 native",
        "pins": "EPD SCLK14/MOSI8/CS45/DC13/RST18/BUSY9 @10 MHz; "
            "CST816S SDA41/SCL42/INT1 @0x15, touch reset through a TCA9555 "
            "expander",
        "evidence": "BoardConfig.h:1672-1673,1679; Ssd1677Driver.cpp:118; "
            "MetalioEInk4Board.h:16",
        "machine_gap": "EPD pin map, a CST816S touch device instead of GT911 "
            "and a modelled TCA9555 GPIO expander",
    },
    "papermono": {
        "chip": "esp32s3", "controller": "SSD1677 silicon behind a dedicated "
            "PaperMono driver with host-authored LUTs",
        "resolution": "800x480, ROTATE_180",
        "pins": "EPD SCLK15/MOSI14/CS16/DC17/BUSY18 @20 MHz, RESET and EPD "
            "power on an M5IOE1 expander; FT5x06/FT6336 SDA47/SCL48/INT4 @0x38",
        "evidence": "BoardConfig.h:1109-1112,1124,1129; PaperMonoDriver.cpp:19-22; "
            "M5Ioe1.h:48-50,54",
        "machine_gap": "180-degree scan order, M5IOE1 expander for reset and "
            "power, FT6336 touch, PSRAM grey planes",
    },
    "eego_a4": {
        "chip": "esp32s3", "controller": "UC8279C",
        "resolution": "768x552 framebuffer (controller 768x600 + 48 pad rows), "
            "bottom-to-top scan",
        "pins": "EPD SCLK42/MOSI45/CS21/DC14/RST13/BUSY41 (active-low), "
            "PWR 6 @20 MHz; GSLX680 SDA2/SCL1/RST3 @0x40",
        "evidence": "BoardConfig.h:1476-1479,1493,1500; Uc8279cA4Driver.h:3-5",
        "machine_gap": "a UC8279C device, a 768x552 geometry, active-low BUSY "
            "and bottom-to-top raster order",
    },
    # inkMOD board slugs
    "x4classic": None,   # same board as x4c above, inkMOD spelling
    "x3x4": None,        # same board as x3-x4 above, inkMOD spelling
}


def expectation_for(board):
    """The documented expectation for a board slug, with inkMOD spellings."""
    aliases = {"x4classic": "x4c", "x3x4": "x3-x4"}
    key = aliases.get(board, board)
    return BOARD_EXPECTATIONS.get(key)


def render_table(rows):
    """Human table; unmeasured cells are '-' / 'n/a', never an invented 0."""
    header = ("BOARD", "CHIP", "KIND", "VERDICT", "FRAMES", "FIRST FRAME",
              "SEC", "MERGE")
    widths = (16, 8, 7, 15, 6, 16, 5, 9)
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*header), fmt.format(*["-" * w for w in widths])]
    for row in rows:
        first = row["first_frame_sha256"] or "-"
        frames = row["frames"] if isinstance(row["frames"], int) else "-"
        seconds = (f"{row['wall_seconds']:.0f}"
                   if isinstance(row["wall_seconds"], (int, float)) else "-")
        merge = "merged" if row["merged_sha256"] else (
            "refused" if row["merge_error"] else "copied")
        lines.append(fmt.format(
            f"{row['vendor']}/{row['board']}"[:16],
            row["chip_from_header"] or "-", row["source_kind"],
            row["verdict"][:15], frames, first[:16], seconds, merge))
    counts = summarise(rows)
    lines.append("")
    lines.append(
        f"{counts['rows']} rows: {counts[PAINTED]} painted, "
        f"{counts[PAINTED_NOTHING]} painted nothing, {counts[DIED]} QEMU died, "
        f"{counts[REFUSED]} refused, {counts[RUNNER_ERROR]} runner error")
    return "\n".join(lines)


# --- fetch role (host, read-only) -----------------------------------------
def fetch_release(repo, tag, boards, dest_dir, *, vendor, log=print):
    """``gh release download`` one asset per board into ``dest_dir``.

    Read-only GitHub API use; nothing is written outside the scratch folder.
    A missing asset is reported as such (the row says "not published") rather
    than skipped.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    results = []
    for board in boards:
        name = asset_name(vendor, tag, board)
        target = dest / name
        if target.is_file() and target.stat().st_size > 0:
            results.append({"board": board, "asset": name, "path": str(target),
                            "status": "cached"})
            continue
        cmd = ["gh", "release", "download", tag, "--repo", repo,
               "--pattern", name, "--dir", str(dest), "--clobber"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  errors="replace", timeout=180,
                                  stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            results.append({"board": board, "asset": name,
                            "status": "runner error: gh is not installed"})
            continue
        except subprocess.TimeoutExpired:
            results.append({"board": board, "asset": name,
                            "status": "runner error: gh timed out"})
            continue
        if proc.returncode == 0 and target.is_file():
            results.append({"board": board, "asset": name, "path": str(target),
                            "status": "downloaded"})
            log(f"  fetched {name}")
        else:
            detail = (proc.stderr or proc.stdout).strip()[:200]
            results.append({"board": board, "asset": name,
                            "status": f"not published ({detail})" if
                            "match" in detail else f"gh failed ({detail})"})
            log(f"  {name}: {results[-1]['status']}")
    return results


# --- boot role (disposable container, reused from boot_matrix) ------------
def default_boot(image, **kwargs):
    """Boot one merged image in its own throwaway container.

    boot_matrix owns the container plumbing (``docker run --rm``, no name, no
    port, every mount read-only); this only points it at the live kernel tree.
    Returns the raw observation dict, never a verdict.
    """
    try:
        import boot_matrix
    except Exception as exc:  # pragma: no cover - import environment
        return {"frames": None, "qemu_exit_status": None, "wall_seconds": None,
                "error": f"boot_matrix unavailable: {exc}"}
    return boot_matrix.boot_one_via_container(
        image, fw_dir=kwargs.get("fw_dir") or Path(image).parent,
        qemu_root=kwargs.get("qemu_root") or DEFAULT_QEMU_ROOT,
        run_seconds=kwargs.get("run_seconds", 120.0),
        linger_seconds=kwargs.get("linger_seconds", 5.0))


DEFAULT_QEMU_ROOT = Path(
    "/Users/yaraslauhancharou/.local/state/ybx-emulator-upstream/"
    "qemu-wf-radio-20261031")

# --- probe role: WHY did a board not paint? -------------------------------
# The frame counter alone says "nothing was painted"; it does not say whether
# the guest ever touched the panel. These QOM counters, plus the guest UART,
# separate "the bootloader refused the image" from "the app is running and
# driving pins this machine does not listen to". Property names are the
# aliases the patched machine exports (qemu hw/xtensa/esp32s3.c:834-881).
PROBE_COUNTERS = ("ybx-epd-frames", "ybx-epd-commands", "ybx-epd-ram-writes",
                  "ybx-epd-nonwhite", "ybx-epd-busy-rise-edges",
                  "ybx-epd-busy-fall-edges", "ybx-epd-deep-sleep",
                  "ybx-epd-cmd24", "ybx-epd-cmd26")
PROBE_UART_BYTES = 4096
# Media the guest role needs, from the read-only QA folder the bridge also
# mounts (the same defaults boot_matrix uses, boot_matrix.py:60-64).
DEFAULT_QA_DIR = Path(
    "/Users/yaraslauhancharou/Projects/YBX/artifacts/"
    "ybx-esp32-system-emulation-20261029/qa-20261029")
DEFAULT_ROM_NAME = "s3-full-rom-generator-test.bin"
DEFAULT_SD_NAME = "ybx-test-sd-fat16-mbr-pristine.img"
CONTAINER_QEMU_PATH = "/qemu/build-ybx/qemu-system-xtensa"


def probe_local(flash, rom, sd_image, qemu_bin, run_seconds, workdir=None):
    """Boot one image here and read the EPD counters + the guest UART.

    Deliberately a separate observation path from boot_matrix.boot_local: the
    verdict comes from the frame counter there, while this answers the
    mechanism question with the numbers the panel device actually saw. The
    image files are copied into a private folder, so the operator's media stay
    read-only even if a guest writes to its flash.
    """
    import shutil
    import tempfile
    import time

    result = {"frames": None, "counters": {}, "uart": None,
              "qemu_exit_status": None, "wall_seconds": None, "error": None}
    temp = tempfile.TemporaryDirectory(prefix="ybx-panel-probe-", dir=workdir)
    root = Path(temp.name)
    process = None
    sock = None
    started = time.monotonic()
    try:
        (root / "rom").mkdir()
        shutil.copyfile(rom, root / "rom" / "esp32s3_rev0_rom.bin")
        shutil.copyfile(flash, root / "flash.bin")
        shutil.copyfile(sd_image, root / "sd.img")
        uart_path = root / "uart.log"
        log_handle = (root / "qemu.log").open("w+")
        process = subprocess.Popen([
            str(qemu_bin), "-machine", X4PRO_MACHINE["qemu_machine"],
            "-m", "8M", "-global", "ssi_psram.is_octal=on",
            "-nographic", "-monitor", "none",
            "-serial", f"file:{uart_path}", "-serial", "null",
            "-serial", "null", "-serial", "null",
            "-qmp", f"unix:{root / 'qmp.sock'},server=on,wait=off",
            "-L", str(root / "rom"),
            "-drive", f"file={root / 'flash.bin'},if=mtd,format=raw",
            "-drive", f"file={root / 'sd.img'},if=sd,format=raw",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=log_handle)
        try:
            import boot_matrix
            _client, _reader, execute = boot_matrix.qmp_connect_unix(
                root / "qmp.sock")
        except Exception as exc:
            result["error"] = f"QMP unavailable: {type(exc).__name__}: {exc}"
            execute = None
        deadline = started + run_seconds
        while execute is not None and time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                frames = boot_matrix.qom_frames(
                    execute, "ybx-epd-frames")
            except Exception:
                break
            result["frames"] = frames
            if frames:
                break  # a painted guest answers the mechanism question itself
            time.sleep(0.5)
        if execute is not None:
            for prop in PROBE_COUNTERS:
                try:
                    result["counters"][prop] = boot_matrix.qom_frames(
                        execute, prop)
                except Exception as exc:
                    result["counters"][prop] = f"unreadable ({type(exc).__name__})"
        result["wall_seconds"] = round(time.monotonic() - started, 1)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        else:
            result["qemu_exit_status"] = process.returncode
        if uart_path.is_file():
            result["uart"] = uart_path.read_bytes()[-PROBE_UART_BYTES:].decode(
                "utf-8", "replace")
    except Exception as exc:  # reported verbatim, never hidden as a verdict
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        temp.cleanup()
    return result


def probe_one_via_container(image, *, rom, sd_image, qemu_bin, run_seconds,
                            qemu_root=DEFAULT_QEMU_ROOT,
                            container_image="ybx-qemu-builder:20261029",
                            tools_dir=None):
    """Run probe_local inside its own throwaway container and read the JSON."""
    tools_dir = Path(tools_dir or Path(__file__).resolve().parent)
    image = Path(image)
    cmd = ["docker", "run", "--rm", "--platform", "linux/arm64",
           "-v", f"{qemu_root}:/qemu:ro",
           "-v", f"{image.parent}:/fw:ro",
           "-v", f"{tools_dir}:/tests:ro",
           "-v", f"{rom.parent}:/qa:ro",
           "-w", "/tmp", container_image,
           "python3", "/tests/panel_matrix.py", "--probe-one",
           "--flash", f"/fw/{image.name}",
           "--rom", f"/qa/{Path(rom).name}",
           "--sd-image", f"/qa/{Path(sd_image).name}",
           "--qemu-bin", "/qemu/build-ybx/qemu-system-xtensa",
           "--run-seconds", str(run_seconds)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              errors="replace", timeout=run_seconds + 180,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return {"frames": None, "error": "docker is not available on this host"}
    except subprocess.TimeoutExpired:
        return {"frames": None, "error": "the probe container timed out"}
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and "counters" in payload:
            return payload
    return {"frames": None,
            "error": f"no probe JSON (docker exit {proc.returncode}): "
                     f"{proc.stderr.strip()[-400:]}"}


# --- matrix ---------------------------------------------------------------
def mechanism_from_probe(chip_matches, frames, counters, uart):
    """Say WHICH step failed, from the probe numbers - never from a guess.

    * ``image-not-this-chip`` - the app was not built for an ESP32-S3, so the
      X4 Pro bootloader never hands control to it;
    * ``no-panel-traffic``    - the guest ran but the panel device saw no
      command at all (the app drives pins this machine does not listen to, or
      a controller whose bus writes do not decode as SSD1677);
    * ``panel-addressed-never-refreshed`` - commands arrived, RAM was written,
      but no frame was committed (wrong LUT / busy polarity / geometry);
    * ``painted``             - at least one frame;
    * ``unknown``             - the runner never got that far.
    """
    def counter(name):
        value = (counters or {}).get(name)
        return value if isinstance(value, int) else 0

    commands = counter("ybx-epd-commands")
    ram_writes = counter("ybx-epd-ram-writes")
    if isinstance(frames, int) and frames > 0:
        return "painted"
    if chip_matches is False:
        return "image-not-this-chip"
    if commands == 0 and ram_writes == 0:
        return "no-panel-traffic"
    return "panel-addressed-never-refreshed"


def run_matrix(scratch, *, releases=DEFAULT_RELEASES, boot=None, head=None,
               base_image=DEFAULT_BASE_IMAGE, run_seconds=120.0,
               linger_seconds=5.0, qemu_root=None, boards=None,
               fetch=True, log=print):
    """fetch -> classify -> merge -> boot for every board of every release."""
    scratch = Path(scratch)
    assets_dir = scratch / "assets"
    merged_dir = scratch / "merged"
    merged_dir.mkdir(parents=True, exist_ok=True)
    boot = boot or default_boot
    rows = []
    for release in releases:
        vendor = release["vendor"]
        repo, tag = release["repo"], release["tag"]
        wanted = [b for b in release["boards"] if not boards or b in boards]
        if fetch:
            fetched = fetch_release(repo, tag, wanted, assets_dir,
                            vendor=vendor, log=log)
        else:
            fetched = [{"board": b, "asset": asset_name(vendor, tag, b),
                        "path": str(assets_dir / asset_name(vendor, tag, b)),
                        "status": "on disk"} for b in wanted]
        for item in fetched:
            board, name = item["board"], item["asset"]
            path = item.get("path")
            if not path or not Path(path).is_file():
                # The release simply does not publish this board, or the
                # download never landed: nothing is booted and nothing is
                # claimed about a guest.
                rows.append({"name": name, "vendor": vendor, "board": board,
                             "source_kind": "unknown",
                             "merge_error": "asset not present in the scratch "
                                            "folder (not published for this "
                                            "tag, or fetch failed)",
                             "verdict": REFUSED,
                             "expectation": expectation_for(board)})
                continue
            source = Path(path)
            data = source.read_bytes() if source.is_file() else b""
            kind = classify_bytes(data)
            header = esp_header(data)
            out = merged_dir / merged_name(board, vendor, tag)
            log(f"  {name}: kind={kind} chip={(header or {}).get('chip')}")
            merge_row = merge_asset(source, out, head=head,
                                    base_image=base_image)
            if not merge_row["ok"]:
                observed = {"frames": None, "qemu_exit_status": None,
                            "wall_seconds": None, "error": None}
            else:
                log(f"  booting {out.name} ...")
                observed = boot(out, fw_dir=merged_dir, run_seconds=run_seconds,
                                linger_seconds=linger_seconds,
                                qemu_root=qemu_root)
            rows.append(row_from_observation(name, board, vendor, source,
                                             observed, merge_row, header))
    return rows


def build_report(rows, scratch, options):
    return {
        "schema": "ybx-panel-matrix/1",
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "scratch": str(scratch),
        "machine": X4PRO_MACHINE,
        "options": options,
        "summary": summarise(rows),
        "results": rows,
    }


# --- CLI ------------------------------------------------------------------
def parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("command", nargs="?", default=None,
                   choices=("classify", "merge", "fetch", "run", "probe",
                            "expectations"))
    p.add_argument("--scratch", type=Path,
                   default=Path(os.environ.get(
                       "YBX_PANEL_MATRIX_SCRATCH",
                       "/Users/yaraslauhancharou/.local/state/"
                       "ybx-panel-matrix-20261031")))
    p.add_argument("--dir", type=Path, help="folder of assets (classify)")
    p.add_argument("--app", type=Path, help="app-only image (merge)")
    p.add_argument("--out", type=Path, help="output image (merge)")
    p.add_argument("--base", type=Path, default=DEFAULT_BASE_IMAGE,
                   help="image whose first 64 KiB are the X4 Pro base")
    p.add_argument("--tag", help="release tag (fetch/run)")
    p.add_argument("--vendor", choices=("crosspoint", "inkmod"))
    p.add_argument("--boards", help="comma-separated board slugs")
    p.add_argument("--no-fetch", action="store_true",
                   help="use the assets already in the scratch folder")
    p.add_argument("--run-seconds", type=float, default=120.0)
    p.add_argument("--linger-seconds", type=float, default=5.0)
    p.add_argument("--qemu-root", type=Path, default=DEFAULT_QEMU_ROOT)
    p.add_argument("--json-out", type=Path)
    p.add_argument("--probe-one", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--flash", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--rom", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--sd-image", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--qemu-bin", type=Path, help=argparse.SUPPRESS)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    wanted = tuple(b.strip() for b in args.boards.split(",")) if args.boards \
        else None
    if args.probe_one:
        missing = [flag for flag, value in (("--flash", args.flash),
                                            ("--rom", args.rom),
                                            ("--sd-image", args.sd_image),
                                            ("--qemu-bin", args.qemu_bin))
                   if value is None]
        if missing:
            print(f"--probe-one needs {', '.join(missing)}", file=sys.stderr)
            return 2
        result = probe_local(args.flash, args.rom, args.sd_image,
                             args.qemu_bin, args.run_seconds)
        print(json.dumps(result))
        return 0 if result.get("error") is None else 1
    if args.command is None:
        print("no command (classify | merge | fetch | run | probe | "
              "expectations)", file=sys.stderr)
        return 2
    if args.command == "classify":
        target = args.dir or (args.scratch / "assets")
        for path in sorted(Path(target).glob("*.bin")):
            kind, data = classify_file(path)
            header = esp_header(data or b"")
            board = board_from_asset_name(path.name)
            print(json.dumps({"file": path.name, "kind": kind,
                              "board": board, "header": header}, indent=2))
        return 0
    if args.command == "merge":
        if not args.app or not args.out:
            print("merge needs --app and --out", file=sys.stderr)
            return 1
        row = merge_asset(args.app, args.out, base_image=args.base)
        print(json.dumps(row, indent=2))
        return 0 if row.get("ok") else 1
    if args.command == "expectations":
        print(json.dumps(BOARD_EXPECTATIONS, indent=2))
        return 0
    if args.command == "fetch":
        releases = [r for r in DEFAULT_RELEASES
                    if (not args.vendor or r["vendor"] == args.vendor)
                    and (not args.tag or r["tag"] == args.tag)]
        out = []
        for release in releases:
            out.append({"release": release["tag"], "vendor": release["vendor"],
                        "assets": fetch_release(release["repo"], release["tag"],
                                                release["boards"] if not wanted
                                                else [b for b in
                                                      release["boards"]
                                                      if b in wanted],
                                                args.scratch / "assets",
                                                vendor=release["vendor"])})
        print(json.dumps(out, indent=2))
        return 0
    if args.command == "probe":
        merged = args.scratch / "merged"
        qa = DEFAULT_QA_DIR
        images = sorted(merged.glob("*.bin")) if merged.is_dir() else []
        if wanted:
            images = [p for p in images if any(b in p.name for b in wanted)]
        out = []
        for image in images:
            observed = probe_one_via_container(
                image, rom=qa / DEFAULT_ROM_NAME, sd_image=qa / DEFAULT_SD_NAME,
                qemu_bin=CONTAINER_QEMU_PATH, run_seconds=args.run_seconds,
                qemu_root=args.qemu_root)
            ident = board_from_merged_name(image.name)
            expectation = expectation_for(ident["board"]) if ident else None
            chip_matches = (expectation["chip"] == X4PRO_MACHINE["chip"]
                            if expectation else None)
            out.append({"image": image.name,
                        "board": (ident or {}).get("board"),
                        "chip_from_sources": (expectation or {}).get("chip"),
                        "chip_matches_machine": chip_matches,
                        "mechanism": "unknown" if observed.get("error")
                        else mechanism_from_probe(
                            chip_matches, observed.get("frames"),
                            observed.get("counters"), observed.get("uart")),
                        "frames": observed.get("frames"),
                        "counters": observed.get("counters"),
                        "uart": observed.get("uart"),
                        "error": observed.get("error"),
                        "wall_seconds": observed.get("wall_seconds")})
            print(json.dumps(out[-1]), file=sys.stderr, flush=True)
        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(out, indent=2) + "\n",
                                     encoding="utf-8")
        for row in out:
            counters = row["counters"] or {}
            print(f"{row['image'][:44]:44} frames={str(row['frames']):>4} "
                  f"commands={counters.get('ybx-epd-commands')} "
                  f"ram_writes={counters.get('ybx-epd-ram-writes')} "
                  f"mechanism={row['mechanism']}")
        return 0

    releases = DEFAULT_RELEASES
    if args.tag or args.vendor:
        releases = [r for r in DEFAULT_RELEASES
                    if (not args.vendor or r["vendor"] == args.vendor)
                    and (not args.tag or r["tag"] == args.tag)]
    rows = run_matrix(args.scratch, releases=releases, boards=wanted,
                      fetch=not args.no_fetch, base_image=args.base,
                      run_seconds=args.run_seconds,
                      linger_seconds=args.linger_seconds,
                      qemu_root=args.qemu_root,
                      log=lambda text: print(text, file=sys.stderr, flush=True))
    report = build_report(rows, args.scratch, {
        "releases": [f"{r['vendor']}:{r['tag']}" for r in releases],
        "boards": wanted, "run_seconds": args.run_seconds,
        "linger_seconds": args.linger_seconds,
        "qemu_root": str(args.qemu_root), "base_head_sha256":
        X4PRO_BASE_HEAD_SHA256, "fetch": not args.no_fetch})
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2) + "\n",
                                 encoding="utf-8")
    print(render_table(rows))
    return 2 if report["summary"][RUNNER_ERROR] else 0


if __name__ == "__main__":
    sys.exit(main())
