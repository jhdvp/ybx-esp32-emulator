"""Host-only tests pinning the X4 Pro flash-image classification and the
boot-expectation matrix measured in
experiments/0068-bootloader-flash-boot-matrix-20261031.md.

Everything here works from image BYTES only (image magic, partition table
magic and contents, app offset, payload-window state). No Docker, no
network, no guest. Artifact-dependent tests are skipped when the QA media
folder is absent; synthetic fixtures always run.

Key pinned facts (batch-4, 2026-10-31):
  * The factory image and every supposedly "CrossPoint bootloader" image
    carry BYTE-IDENTICAL bootloader bytes at [0x0, 0x8000) - the QEMU
    SIGSEGV crash class was never caused by a different bootloader
    (0068 report, sections 2-4).
  * expB-crosspoint-bl-factory-app.bin equals the factory flash image
    except for 78 erased bytes in the nvs/otadata/phy payload windows;
    swapping exactly those 78 bytes reproduces the factory SHA.
  * A "not a flash image" (app-only bin) must be rejected via the
    partition-table magic at 0x8000, not via the header magic at 0x0.

The runtime behaviour column (measured on a QEMU build patched with
experiments/0068-esp32s3-bootloader-flash-fix.patch) documents expectations
only; these host tests never launch QEMU.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import struct
import tempfile
import unittest

ARTIFACTS = Path(os.environ.get(
    "YBX_QA_ARTIFACTS",
    "/Users/yaraslauhancharou/Projects/YBX/artifacts/"
    "ybx-esp32-system-emulation-20261029"))
QA = ARTIFACTS / "qa-20261029"
FW = ARTIFACTS / "firmware"

BOOT_MAGIC = 0xE9
PARTITION_MAGIC = 0x50AA
PARTITION_OFFSET = 0x8000
APP_OFFSET = 0x10000
BOOTLOADER_REGION = (0x0000, 0x8000)
ESP32S3_CHIP_ID = 0x0009
FACTORY_BOOTLOADER_SHA256 = (
    "6e513914db2824287dfba41d90a6bf2907bae72cdcdd5fe12b97a80bb1341533")
FACTORY_IMAGE_SHA256 = (
    "206ed9661bd71477d1a54397c5a2cfe490179a3f59ab094f1e7421745c609523")

# The 10 byte-ranges [start, end) where the factory image and
# expB-crosspoint-bl-factory-app.bin differ (78 bytes total, 2026-10-31).
SEVENTY_EIGHT_RANGES = [
    (0x9000, 0x9001), (0x9004, 0x9009), (0x901c, 0x9021),
    (0x9040, 0x9043), (0x9044, 0x9059), (0x9060, 0x9063),
    (0x9064, 0x9080), (0xe000, 0xe004), (0xe01c, 0xe020),
    (0xf000, 0xf004),
]

EXPECTED_PARTITIONS = [
    (1, 2, 0x9000, 0x5000, "nvs"),
    (1, 0, 0xE000, 0x2000, "otadata"),
    (0, 16, 0x10000, 0x640000, "app0"),
    (0, 17, 0x650000, 0x640000, "app1"),
    (1, 130, 0xC90000, 0x360000, "spiffs"),
    (1, 3, 0xFF0000, 0x10000, "coredump"),
]

# name -> (path, classification, payload state, measured QEMU behaviour).
# "app_silent": app runs but prints nothing on UART0 (reference images).
# "app_with_nvs_errors": app runs and prints Preferences/nvs errors.
# "silent_before_app": no output beyond the ROM bootloader entry line.
# No class may crash the QEMU host any more once
# 0068-esp32s3-bootloader-flash-fix.patch is applied (measured 9/9).
BOOT_MATRIX = {
    "s3-flash-complete-rom-test.bin":
        (QA, "merged", "prewarmed", "app_silent"),
    "ybx-ui-c663d6a0-on-factory-base.bin":
        (FW, "merged", "prewarmed", "app_silent"),
    "controls/expA-factory-bl-crosspoint2-app.bin":
        (FW, "merged", "prewarmed", "silent_before_app"),
    "controls/expB-crosspoint-bl-factory-app.bin":
        (FW, "merged", "erased-base", "app_with_nvs_errors"),
    "crosspoint-2.0.0rc-x4pro-flash-16m.bin":
        (FW, "merged", "erased-base", "app_with_nvs_errors"),
    "crosspoint-1.6.5-x4pro-flash-16m.bin":
        (FW, "merged", "erased-base", "silent_before_app"),
    "inkmod-v1.1.10-x4pro-flash-16m.bin":
        (FW, "merged", "erased-base", "silent_before_app"),
    "controls/YBX-UI-UNRELEASED-x4pro-c663d6a0-factory-16m.bin":
        (FW, "merged", "nvs-erased-ota-written", "app_with_nvs_errors"),
    "controls/YBX-SETTINGS-UNRELEASED-x4pro-092c2be5-factory-16m.bin":
        (FW, "merged", "nvs-erased-ota-written", "app_with_nvs_errors"),
}
REJECTED_IMAGES = {
    # app-only image: partition window holds "rft" text (magic 0x2974),
    # 0x10000 holds magic 0x73 -> not a merged flash image at all.
    "YBX-UI-UNRELEASED-x4pro-c663d6a0.bin": "rejected:partition-table-magic",
}


def _read_at(fh, offset: int, size: int) -> bytes:
    fh.seek(offset)
    data = fh.read(size)
    if len(data) != size:
        raise ValueError(f"short read at 0x{offset:x}")
    return data


def parse_image_header(block: bytes, offset: int = 0) -> dict:
    """Decode an ESP32-S3 image header from `offset` within `block`."""
    return {
        "magic": block[offset],
        "segments": block[offset + 1],
        "flash_config": block[offset + 2],   # low nibble speed, high size
        "spi_pin": block[offset + 3],
        "entry": struct.unpack_from("<I", block, offset + 4)[0],
        "chip_id": struct.unpack_from("<H", block, offset + 12)[0],
    }


def parse_partition_table(block: bytes) -> list[dict]:
    """Walk 32-byte partition records starting at 0x8000 within `block`."""
    parts = []
    i = PARTITION_OFFSET
    while i + 32 <= len(block):
        magic, type_, subtype, off, size = struct.unpack_from("<HBBII", block, i)
        if magic != PARTITION_MAGIC:
            break
        label = block[i + 12:i + 28].split(b"\x00")[0].decode("ascii")
        parts.append({"type": type_, "subtype": subtype, "offset": off,
                      "size": size, "label": label})
        i += 32
    return parts


def classify_flash_image(path: Path) -> str:
    """Return 'merged' for a bootloader+partitions+app flash image, else
    'rejected:...' naming the first failing byte-level criterion."""
    with open(path, "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        if size < APP_OFFSET + 16:
            return "rejected:too-small-for-flash-image"
        boot = parse_image_header(_read_at(fh, 0, 16))
        if boot["magic"] != BOOT_MAGIC:
            return "rejected:bootloader-magic"
        fh.seek(PARTITION_OFFSET)
        magic_bytes = fh.read(2)
        if len(magic_bytes) < 2:
            return "rejected:missing-partition-table"
        if struct.unpack("<H", magic_bytes)[0] != PARTITION_MAGIC:
            return "rejected:partition-table-magic"
        app = parse_image_header(_read_at(fh, APP_OFFSET, 16))
        if app["magic"] != BOOT_MAGIC:
            return "rejected:app-magic-at-0x10000"
    return "merged"


def base_payload_state(path: Path) -> str:
    """State of the three payload windows that decide whether the guest
    re-initialises NVS/otadata during boot (the crash trigger of 0068)."""
    with open(path, "rb") as fh:
        nvs = _read_at(fh, 0x9000, 1)[0]
        ota = _read_at(fh, 0xe000, 4)
    if nvs != 0xFF:
        return "prewarmed"
    if ota == b"\xff\xff\xff\xff":
        return "erased-base"
    return "nvs-erased-ota-written"


def _partition_record(type_: int, subtype: int, offset: int, size: int,
                      label: str) -> bytes:
    packed = struct.pack("<HBBII", PARTITION_MAGIC, type_, subtype,
                         offset, size)
    raw = label.encode("ascii")
    return packed + raw + bytes(16 - len(raw))


def _build_synthetic_flash(total_size: int = 0x10010) -> bytes:
    img = bytearray(b"\xff" * max(total_size, APP_OFFSET + 16))
    boot = bytearray(16)
    boot[0] = BOOT_MAGIC
    boot[1] = 3
    boot[2] = 0x02
    boot[3] = 0x4f
    struct.pack_into("<I", boot, 4, 0x403C8898)
    struct.pack_into("<H", boot, 12, ESP32S3_CHIP_ID)
    img[0:16] = boot
    nvs_rec = _partition_record(1, 2, 0x9000, 0x5000, "nvs")
    app_rec = _partition_record(0, 16, 0x10000, 0x640000, "app0")
    img[0x8000:0x8000 + len(nvs_rec)] = nvs_rec
    img[0x8020:0x8020 + len(app_rec)] = app_rec
    app = bytearray(16)
    app[0] = BOOT_MAGIC
    app[1] = 7
    struct.pack_into("<I", app, 4, 0x40376134)
    img[APP_OFFSET:APP_OFFSET + 16] = app
    return bytes(img[:total_size])


ARTIFACTS_READY = FW.is_dir() and QA.is_dir()


@unittest.skipUnless(ARTIFACTS_READY, f"QA artifacts missing under {ARTIFACTS}")
class TestBootloaderByteIdentity(unittest.TestCase):
    """The crash class was attributed to a 'CrossPoint bootloader'; the bytes
    say every X4 Pro merged image carries the SAME bootloader."""

    MERGED = [
        QA / "s3-flash-complete-rom-test.bin",
        FW / "controls/expB-crosspoint-bl-factory-app.bin",
        FW / "controls/expA-factory-bl-crosspoint2-app.bin",
        FW / "crosspoint-2.0.0rc-x4pro-flash-16m.bin",
        FW / "crosspoint-1.6.5-x4pro-flash-16m.bin",
        FW / "inkmod-v1.1.10-x4pro-flash-16m.bin",
        FW / "ybx-ui-c663d6a0-on-factory-base.bin",
    ]

    def test_bootloader_region_is_identical(self):
        for path in self.MERGED:
            with self.subTest(path.name):
                with open(path, "rb") as fh:
                    region = _read_at(fh, *BOOTLOADER_REGION)
                self.assertEqual(
                    hashlib.sha256(region).hexdigest(),
                    FACTORY_BOOTLOADER_SHA256)

    def test_boot_header_fields(self):
        for path in self.MERGED:
            with self.subTest(path.name):
                with open(path, "rb") as fh:
                    boot = parse_image_header(_read_at(fh, 0, 16))
                self.assertEqual(boot["magic"], BOOT_MAGIC)
                self.assertEqual(boot["chip_id"], ESP32S3_CHIP_ID)
                self.assertEqual(boot["entry"], 0x403C8898)
                self.assertEqual(boot["flash_config"], 0x02)

    def test_partition_table_entries_match_across_all_classes(self):
        for path in self.MERGED:
            with self.subTest(path.name):
                with open(path, "rb") as fh:
                    block = _read_at(fh, 0, PARTITION_OFFSET + 0x1000)
                parts = parse_partition_table(block)
                got = [(p["type"], p["subtype"], p["offset"], p["size"],
                        p["label"]) for p in parts]
                self.assertEqual(got, EXPECTED_PARTITIONS)
                self.assertEqual(parts[2]["offset"], APP_OFFSET)


@unittest.skipUnless(ARTIFACTS_READY, f"QA artifacts missing under {ARTIFACTS}")
class TestSeventyEightByteCorrelation(unittest.TestCase):
    """The factory image and expB differ ONLY in 78 erased payload bytes;
    boot vs host-SIGSEGV followed those bytes, not the bootloader."""

    def test_diff_is_exactly_the_pinned_ranges(self):
        factory = (QA / "s3-flash-complete-rom-test.bin").read_bytes()
        expb = (FW / "controls/expB-crosspoint-bl-factory-app.bin").read_bytes()
        self.assertEqual(len(factory), len(expb))
        ranges = [j for b in range(0, len(factory), 64)
                  if factory[b:b + 64] != expb[b:b + 64]
                  for j in range(b, b + 64) if factory[j] != expb[j]]
        self.assertEqual(len(ranges), 78, "diff byte count changed")
        groups = []
        start = prev = ranges[0]
        for j in ranges[1:]:
            if j != prev + 1:
                groups.append((start, prev + 1))
                start = j
            prev = j
        groups.append((start, prev + 1))
        self.assertEqual(groups, SEVENTY_EIGHT_RANGES)
        for s, e in groups:
            self.assertEqual(expb[s:e], b"\xff" * (e - s),
                             f"erased side must be expB in 0x{s:x}")

    def test_swapping_the_78_bytes_reproduces_the_other_image(self):
        factory = (QA / "s3-flash-complete-rom-test.bin").read_bytes()
        expb = bytearray((FW / "controls/expB-crosspoint-bl-factory-app.bin"
                          ).read_bytes())
        for s, e in SEVENTY_EIGHT_RANGES:
            expb[s:e] = factory[s:e]
        self.assertEqual(hashlib.sha256(bytes(expb)).hexdigest(),
                         FACTORY_IMAGE_SHA256)


@unittest.skipUnless(ARTIFACTS_READY, f"QA artifacts missing under {ARTIFACTS}")
class TestClassificationMatrix(unittest.TestCase):
    """Byte shape + payload state + measured QEMU behaviour for every
    shipped candidate (the 0068 boot matrix)."""

    def test_matrix_rows(self):
        for name, (root, shape, payload, behaviour) in sorted(
                BOOT_MATRIX.items()):
            path = root / name
            with self.subTest(name):
                self.assertTrue(path.is_file(), f"missing {path}")
                self.assertEqual(classify_flash_image(path), shape)
                self.assertEqual(base_payload_state(path), payload)
                self.assertIn(behaviour, ("app_silent", "app_with_nvs_errors",
                                          "silent_before_app"))

    def test_rejected_candidates(self):
        for name, verdict in sorted(REJECTED_IMAGES.items()):
            with self.subTest(name):
                self.assertEqual(classify_flash_image(FW / name), verdict)

    def test_partition_tables_have_no_md5_or_flags_extension(self):
        # every record's trailing 4 bytes (post-label) are zero in all
        # shipped shapes; the next person gets a hard pin if that changes
        for name, (root, _, _, _) in sorted(BOOT_MATRIX.items()):
            with self.subTest(name):
                with open(root / name, "rb") as fh:
                    block = _read_at(fh, 0, PARTITION_OFFSET + 0x1000)
                for p in range(16):
                    rec = block[PARTITION_OFFSET + p * 32:
                                PARTITION_OFFSET + (p + 1) * 32]
                    if struct.unpack_from("<H", rec, 0)[0] != PARTITION_MAGIC:
                        break
                    self.assertEqual(rec[28:32], b"\x00\x00\x00\x00")


class TestSyntheticFixtures(unittest.TestCase):
    """Byte-shape rules that must hold even without the QA media folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ybx-0068-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, data: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(data)
        return path

    def test_synthetic_merged_image(self):
        path = self._write("synth.bin", _build_synthetic_flash())
        self.assertEqual(classify_flash_image(path), "merged")
        with open(path, "rb") as fh:
            block = _read_at(fh, 0, PARTITION_OFFSET + 0x1000)
        parts = parse_partition_table(block)
        self.assertEqual([p["label"] for p in parts], ["nvs", "app0"])
        self.assertEqual(parts[1]["offset"], APP_OFFSET)
        boot = parse_image_header(block)
        self.assertEqual(boot["chip_id"], ESP32S3_CHIP_ID)
        self.assertEqual(boot["entry"], 0x403C8898)

    def test_bad_partition_magic_rejected(self):
        data = bytearray(_build_synthetic_flash())
        struct.pack_into("<H", data, PARTITION_OFFSET, 0x2974)
        path = self._write("badpt.bin", bytes(data))
        self.assertEqual(classify_flash_image(path),
                         "rejected:partition-table-magic")

    def test_bad_boot_magic_rejected(self):
        data = bytearray(_build_synthetic_flash())
        data[0] = 0x00
        path = self._write("badboot.bin", bytes(data))
        self.assertEqual(classify_flash_image(path),
                         "rejected:bootloader-magic")

    def test_bad_app_magic_at_offset_rejected(self):
        data = bytearray(_build_synthetic_flash())
        data[APP_OFFSET] = 0x73
        path = self._write("badapp.bin", bytes(data))
        self.assertEqual(classify_flash_image(path),
                         "rejected:app-magic-at-0x10000")

    def test_truncated_file_rejected(self):
        path = self._write("tiny.bin", _build_synthetic_flash(0x10000))
        self.assertEqual(classify_flash_image(path),
                         "rejected:too-small-for-flash-image")

    def test_payload_states_on_synthetics(self):
        blank = bytearray(_build_synthetic_flash())
        self.assertEqual(base_payload_state(
            self._write("blank.bin", bytes(blank))), "erased-base")
        pre = bytearray(blank)
        pre[0x9000] = 0xFE
        self.assertEqual(base_payload_state(
            self._write("pre.bin", bytes(pre))), "prewarmed")
        mixed = bytearray(blank)
        mixed[0xe000:0xe004] = struct.pack("<I", 1)
        self.assertEqual(base_payload_state(
            self._write("mixed.bin", bytes(mixed))),
            "nvs-erased-ota-written")


if __name__ == "__main__":
    unittest.main()
