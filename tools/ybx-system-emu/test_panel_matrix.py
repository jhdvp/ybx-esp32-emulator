#!/usr/bin/env python3
"""Host-only tests for panel_matrix: byte classification, merge offsets,
verdict mapping and the board-expectation table.

No network, no Docker, no guest: the boot step is injected, and the real
release assets are used only when the scratch folder happens to be present
(otherwise those tests skip and say so).

Pinned facts these tests protect (measured 2026-10-31, see
experiments/0083-panel-matrix-20261031.md):
  * every published CrossPoint/inkMOD board asset is an APP-ONLY image: magic
    0xE9 at 0x0, no partition table at 0x8000 - the bridge refuses all of them;
  * merging an app image at 0x10000 under the X4 Pro head (0x0..0x8000
    bootloader, 0x8000..0x10000 partitions) padded to 16 MiB with 0xFF
    reproduces firmware/crosspoint-2.0.0rc-x4pro-flash-16m.bin,
    crosspoint-1.6.5-x4pro-flash-16m.bin and inkmod-v1.1.10-x4pro-flash-16m.bin
    BYTE FOR BYTE;
  * the ESP chip_id at header offset 0xC separates x3-x4 (0x0005, ESP32-C3)
    from every other board (0x0009, ESP32-S3);
  * a RISC-V ESP32-C3 image cannot execute on the xtensa-only X4 Pro machine,
    so its verdict is decided by the bytes before any boot is attempted.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest

import panel_matrix as pm

ARTIFACTS = Path(os.environ.get(
    "YBX_QA_ARTIFACTS",
    "/Users/yaraslauhancharou/Projects/YBX/artifacts/"
    "ybx-esp32-system-emulation-20261029"))
FIRMWARE = ARTIFACTS / "firmware"
SCRATCH = Path(os.environ.get(
    "YBX_PANEL_MATRIX_SCRATCH",
    "/Users/yaraslauhancharou/.local/state/ybx-panel-matrix-20261031"))
ASSETS = SCRATCH / "assets"

APP = bytes([0xE9, 0x02, 0x02, 0x07]) + b"\x00" * 4      # magic + header
FLASHISH = bytearray(b"\xff" * 0x11000)
FLASHISH[0] = 0xE9
FLASHISH[0x8000:0x8002] = pm.PARTITION_MAGIC
FLASHISH[0x10000] = 0xE9


def app_image(size=0x400, chip_id=0x0009, entry=0x4037_6134):
    """A synthetic but structurally valid ESP app-only image."""
    data = bytearray(b"\xff" * max(size, 0x200))
    data[0] = pm.IMAGE_MAGIC
    data[1] = 7
    data[2] = 2
    data[3] = 0x4F
    data[4:8] = entry.to_bytes(4, "little")
    data[0xC:0xE] = chip_id.to_bytes(2, "little")
    return bytes(data)


class ClassificationTest(unittest.TestCase):
    def test_app_only_asset_is_app(self):
        self.assertEqual(pm.classify_bytes(app_image()), "app")

    def test_complete_flash_image_is_flash(self):
        self.assertEqual(pm.classify_bytes(bytes(FLASHISH)), "flash")

    def test_mbr_is_sd(self):
        data = bytearray(b"\x00" * 0x300)
        data[0x1FE:0x200] = pm.MBR_SIGNATURE
        self.assertEqual(pm.classify_bytes(bytes(data)), "sd")

    def test_short_or_bogus_bytes_are_unknown(self):
        self.assertEqual(pm.classify_bytes(b""), "unknown")
        self.assertEqual(pm.classify_bytes(b"\x00" * 0x400), "unknown")
        self.assertEqual(pm.classify_bytes(b"PK\x03\x04" + b"\x00" * 0x400),
                         "unknown")

    def test_partition_magic_without_app_magic_is_app_not_flash(self):
        # A bootloader-looking image with a partition table but garbage at
        # 0x10000 is NOT bootable: it must not be classified "flash".
        data = bytearray(b"\xff" * 0x11000)
        data[0] = pm.IMAGE_MAGIC
        data[0x8000:0x8002] = pm.PARTITION_MAGIC
        self.assertEqual(pm.classify_bytes(bytes(data)), "app")

    def test_unreadable_file_is_unknown_not_a_crash(self):
        kind, data = pm.classify_file(Path("/nonexistent/nope.bin"))
        self.assertEqual(kind, "unknown")
        self.assertIsNone(data)


class EspHeaderTest(unittest.TestCase):
    def test_chip_id_and_entry_are_read(self):
        header = pm.esp_header(app_image(chip_id=0x0009))
        self.assertEqual(header["chip"], "esp32s3")
        self.assertEqual(header["entry_addr"], 0x4037_6134)
        self.assertEqual(header["flash_mode"], 2)
        self.assertEqual(header["segment_count"], 7)

    def test_c3_asset_reports_c3(self):
        header = pm.esp_header(app_image(chip_id=0x0005, entry=0x4038_0994))
        self.assertEqual(header["chip"], "esp32c3")

    def test_unknown_chip_id_is_none_not_a_guess(self):
        self.assertIsNone(pm.esp_header(app_image(chip_id=0xBEEF))["chip"])

    def test_non_esp_bytes_have_no_header(self):
        self.assertIsNone(pm.esp_header(b"\x00" * 0x400))
        self.assertIsNone(pm.esp_header(b""))

    def test_sha_and_size_describe_the_same_bytes(self):
        data = app_image(size=0x500)
        header = pm.esp_header(data)
        self.assertEqual(header["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(header["size_bytes"], len(data))


class AssetNameTest(unittest.TestCase):
    def test_crosspoint_and_inkmod_spellings(self):
        self.assertEqual(pm.board_from_asset_name(
            "crosspoint-2.0.0rc-metalio_eink4.bin"),
            {"vendor": "crosspoint", "board": "metalio_eink4"})
        self.assertEqual(pm.board_from_asset_name(
            "crosspoint-1.6.5-x3-x4.bin"),
            {"vendor": "crosspoint", "board": "x3-x4"})
        self.assertEqual(pm.board_from_asset_name(
            "firmware-x4classic-v1.1.10.bin"),
            {"vendor": "inkmod", "board": "x4classic"})

    def test_unattributed_name_returns_none(self):
        self.assertIsNone(pm.board_from_asset_name("s3-flash-complete.bin"))

    def test_asset_name_round_trips(self):
        for board in pm.CROSSPOINT_BOARDS:
            name = pm.asset_name("crosspoint", "2.0.0rc", board)
            self.assertEqual(pm.board_from_asset_name(name)["board"], board)
        for board in pm.INKMOD_BOARDS:
            name = pm.asset_name("inkmod", "v1.1.10", board)
            self.assertEqual(pm.board_from_asset_name(name)["board"], board)


class MergeTest(unittest.TestCase):
    def setUp(self):
        head = bytearray(pm.APP_OFFSET)
        head[0] = pm.IMAGE_MAGIC
        head[pm.PARTITION_OFFSET:pm.PARTITION_OFFSET + 2] = pm.PARTITION_MAGIC
        self.head = bytes(head)

    def test_layout_offsets(self):
        image = pm.build_flash_image(self.head, app_image(0x300))
        self.assertEqual(len(image), pm.FLASH_SIZE)
        self.assertEqual(image[0:pm.APP_OFFSET], self.head)
        self.assertEqual(image[pm.APP_OFFSET:pm.APP_OFFSET + 0x300],
                         app_image(0x300))
        self.assertEqual(set(image[pm.APP_OFFSET + 0x300:]), {pm.ERASED_BYTE})

    def test_head_must_land_exactly_on_the_app_offset(self):
        with self.assertRaises(pm.MergeError):
            pm.build_flash_image(self.head[:-1], app_image())
        with self.assertRaises(pm.MergeError):
            pm.build_flash_image(self.head + b"\x00", app_image())

    def test_app_without_magic_is_refused(self):
        with self.assertRaises(pm.MergeError):
            pm.build_flash_image(self.head, b"\x00" * 0x400)

    def test_a_complete_flash_image_is_never_merged_twice(self):
        with self.assertRaises(pm.MergeError):
            pm.build_flash_image(self.head, bytes(FLASHISH))

    def test_oversized_app_is_refused_not_truncated(self):
        too_big = app_image(0x1000) + b"\xff" * (pm.FLASH_SIZE - pm.APP_OFFSET)
        with self.assertRaises(pm.MergeError) as caught:
            pm.build_flash_image(self.head, too_big)
        self.assertIn("does not fit", str(caught.exception))

    def test_padding_is_0xff_not_0x00(self):
        image = pm.build_flash_image(self.head, app_image(0x200))
        self.assertEqual(image[pm.FLASH_SIZE - 1], pm.ERASED_BYTE)


class BaseHeadTest(unittest.TestCase):
    def test_head_is_64k_and_validated(self):
        if not pm.DEFAULT_BASE_IMAGE.is_file():
            self.skipTest(f"base image absent: {pm.DEFAULT_BASE_IMAGE}")
        head = pm.base_head()
        self.assertEqual(len(head), pm.APP_OFFSET)
        self.assertEqual(head[pm.PARTITION_OFFSET:pm.PARTITION_OFFSET + 2],
                         pm.PARTITION_MAGIC)
        self.assertEqual(hashlib.sha256(head).hexdigest(),
                         pm.X4PRO_BASE_HEAD_SHA256)

    def test_a_wrong_base_is_refused_by_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "base.bin"
            data = bytearray(0x11000)
            data[0] = pm.IMAGE_MAGIC
            data[pm.PARTITION_OFFSET:pm.PARTITION_OFFSET + 2] = \
                pm.PARTITION_MAGIC
            path.write_bytes(bytes(data))
            with self.assertRaises(pm.MergeError) as caught:
                pm.base_head(path)
            self.assertIn("SHA-256", str(caught.exception))

    def test_missing_base_says_so(self):
        with self.assertRaises(pm.MergeError):
            pm.base_head(Path("/nonexistent/base.bin"))


class ReproductionTest(unittest.TestCase):
    """The merge rule must rebuild the three images the firmware folder ships."""

    KNOWN = [("crosspoint-2.0.0rc-x4pro-flash-16m.bin",
              "crosspoint-2.0.0rc-x4pro.bin"),
             ("crosspoint-1.6.5-x4pro-flash-16m.bin",
              "crosspoint-1.6.5-x4pro.bin"),
             ("inkmod-v1.1.10-x4pro-flash-16m.bin",
              "firmware-x4pro-v1.1.10.bin")]

    def test_merged_image_equals_the_shipped_one(self):
        if not pm.DEFAULT_BASE_IMAGE.is_file():
            self.skipTest("firmware folder not mounted")
        head = pm.base_head()
        for target_name, asset_name in self.KNOWN:
            target = FIRMWARE / target_name
            asset = ASSETS / asset_name
            if not target.is_file() or not asset.is_file():
                continue
            rebuilt = pm.build_flash_image(head, asset.read_bytes())
            self.assertEqual(
                hashlib.sha256(rebuilt).hexdigest(),
                hashlib.sha256(target.read_bytes()).hexdigest(),
                f"merge rule does not reproduce {target_name}")

    def test_shipped_assets_are_app_only(self):
        if not ASSETS.is_dir():
            self.skipTest(f"scratch assets absent: {ASSETS}")
        checked = 0
        for path in sorted(ASSETS.glob("*.bin")):
            if pm.board_from_asset_name(path.name) is None:
                continue
            self.assertEqual(pm.classify_file(path)[0], "app", path.name)
            checked += 1
        if checked == 0:
            self.skipTest("no board assets downloaded")


class MechanismTest(unittest.TestCase):
    """probe_local's numbers -> an honest statement about WHICH step failed."""

    def counters(self, **kw):
        base = {name: 0 for name in pm.PROBE_COUNTERS}
        base.update(kw)
        return base

    def test_round_trip_of_the_merged_name(self):
        for board in pm.CROSSPOINT_BOARDS + pm.INKMOD_BOARDS:
            name = pm.merged_name(board, "crosspoint", "2.0.0rc")
            self.assertEqual(pm.board_from_merged_name(name)["board"], board)
        self.assertIsNone(pm.board_from_merged_name("some-other.bin"))

    def test_painted(self):
        self.assertEqual(pm.mechanism_from_probe(True, 3, self.counters(), ""),
                         "painted")

    def test_other_chip_wins_over_zero_counters(self):
        self.assertEqual(pm.mechanism_from_probe(False, 0, self.counters(), ""),
                         "image-not-this-chip")

    def test_no_command_and_no_ram_write_is_no_panel_traffic(self):
        self.assertEqual(pm.mechanism_from_probe(True, 0, self.counters(), ""),
                         "no-panel-traffic")

    def test_commands_without_a_frame_is_addressed_but_not_refreshed(self):
        counters = self.counters(**{"ybx-epd-commands": 512,
                                    "ybx-epd-ram-writes": 48000})
        self.assertEqual(pm.mechanism_from_probe(True, 0, counters, ""),
                         "panel-addressed-never-refreshed")

    def test_a_non_numeric_counter_is_treated_as_zero_not_a_crash(self):
        counters = {"ybx-epd-commands": "unreadable (RuntimeError)",
                    "ybx-epd-ram-writes": None}
        self.assertEqual(pm.mechanism_from_probe(True, 0, counters, ""),
                         "no-panel-traffic")

    def test_missing_counters_do_not_claim_a_painted_guest(self):
        self.assertEqual(pm.mechanism_from_probe(None, None, {}, None),
                         "no-panel-traffic")


class VerdictTest(unittest.TestCase):
    def test_painted_needs_a_frame_counter(self):
        self.assertEqual(pm.classify_outcome("app", 3, None), pm.PAINTED)

    def test_zero_frames_live_guest_is_painted_nothing(self):
        self.assertEqual(pm.classify_outcome("app", 0, None),
                         pm.PAINTED_NOTHING)

    def test_exit_status_is_death(self):
        self.assertEqual(pm.classify_outcome("app", 0, -11), pm.DIED)
        self.assertEqual(pm.classify_outcome("app", None, 1), pm.DIED)

    def test_unmergeable_bytes_are_refused_without_a_boot(self):
        self.assertEqual(pm.classify_outcome("unknown", None, None,
                                             merge_error="not an ESP image"),
                         pm.REFUSED)
        self.assertEqual(pm.classify_outcome("sd", None, None), pm.REFUSED)

    def test_runner_error_is_never_a_guest_verdict(self):
        self.assertEqual(pm.classify_outcome("app", 12, None,
                                             error="docker is not available"),
                         pm.RUNNER_ERROR)

    def test_no_observation_at_all_is_a_runner_error(self):
        self.assertEqual(pm.classify_outcome("app", None, None),
                         pm.RUNNER_ERROR)

    def test_merge_error_wins_over_a_stale_frame_count(self):
        # Defensive: an observation that leaked in with a failed merge must not
        # be reported as a painted board.
        self.assertEqual(pm.classify_outcome("app", 5, None,
                                             merge_error="does not fit"),
                         pm.REFUSED)


class RowTest(unittest.TestCase):
    def observation(self, **kw):
        base = {"frames": None, "first_frame_sha256": None,
                "first_frame_seconds": None, "last_frame_sha256": None,
                "wall_seconds": None, "qemu_exit_status": None,
                "qemu_log_tail": None, "error": None}
        base.update(kw)
        return base

    def test_painted_row_carries_the_frame_sha(self):
        header = pm.esp_header(app_image())
        row = pm.row_from_observation(
            "a.bin", "x4c", "crosspoint", "/tmp/a.bin",
            self.observation(frames=4, first_frame_sha256="ab" * 32,
                             last_frame_sha256="cd" * 32,
                             first_frame_seconds=8.2, wall_seconds=12.0),
            {"source_kind": "app", "ok": True, "output": "/tmp/a-flash.bin",
             "merged_sha256": "ef" * 32}, header)
        self.assertEqual(row["verdict"], pm.PAINTED)
        self.assertEqual(row["first_frame_sha256"], "ab" * 32)
        self.assertEqual(row["chip_from_header"], "esp32s3")
        self.assertTrue(row["chip_matches_machine"])
        self.assertEqual(row["entry_addr"], "0x40376134")
        self.assertIsNotNone(row["expectation"])

    def test_a_c3_asset_is_flagged_as_not_this_machine(self):
        # The x3-x4 row: RISC-V code cannot run on the xtensa X4 Pro machine,
        # and the bytes say so before any boot is attempted.
        header = pm.esp_header(app_image(chip_id=0x0005, entry=0x4038_0994))
        row = pm.row_from_observation(
            "x.bin", "x3-x4", "crosspoint", "/tmp/x.bin",
            self.observation(frames=0, qemu_exit_status=None, wall_seconds=90),
            {"source_kind": "app", "ok": True, "output": "/tmp/x-flash.bin",
             "merged_sha256": "12" * 32}, header)
        self.assertFalse(row["chip_matches_machine"])
        self.assertEqual(row["chip_from_header"], "esp32c3")
        self.assertEqual(row["verdict"], pm.PAINTED_NOTHING)

    def test_refused_row_has_no_frame_fields(self):
        row = pm.row_from_observation(
            "a.bin", "x3-x4", "crosspoint", "/tmp/a.bin", self.observation(),
            {"source_kind": "unknown", "ok": False,
             "error": "not an ESP application image"}, None)
        self.assertEqual(row["verdict"], pm.REFUSED)
        self.assertIsNone(row["frames"])
        self.assertIsNone(row["first_frame_sha256"])
        self.assertIsNone(row["last_frame_sha256"])

    def test_summary_counts_every_verdict_once(self):
        rows = [{"verdict": pm.PAINTED}, {"verdict": pm.PAINTED},
                {"verdict": pm.REFUSED}, {"verdict": pm.DIED}]
        counts = pm.summarise(rows)
        self.assertEqual(counts[pm.PAINTED], 2)
        self.assertEqual(counts[pm.REFUSED], 1)
        self.assertEqual(counts[pm.DIED], 1)
        self.assertEqual(counts["rows"], 4)


class ExpectationTableTest(unittest.TestCase):
    def test_every_published_board_has_an_expectation(self):
        for board in pm.CROSSPOINT_BOARDS:
            entry = pm.expectation_for(board)
            self.assertIsNotNone(entry, board)
            for field in ("chip", "controller", "resolution", "pins",
                          "evidence", "machine_gap"):
                self.assertTrue(entry.get(field), f"{board}.{field}")

    def test_inkmod_slugs_alias_to_the_same_board_rows(self):
        self.assertEqual(pm.expectation_for("x4classic"),
                         pm.expectation_for("x4c"))
        self.assertEqual(pm.expectation_for("x3x4"),
                         pm.expectation_for("x3-x4"))
        self.assertEqual(pm.expectation_for("x4pro")["chip"], "esp32s3")

    def test_only_x4pro_claims_no_machine_gap(self):
        for board in pm.CROSSPOINT_BOARDS:
            gap = pm.expectation_for(board)["machine_gap"]
            if board == "x4pro":
                self.assertIn("none", gap.lower())
            else:
                self.assertNotIn("none - this is the machine", gap)

    def test_x3_x4_is_the_only_c3_board(self):
        self.assertEqual(pm.expectation_for("x3-x4")["chip"], "esp32c3")
        for board in pm.CROSSPOINT_BOARDS:
            if board != "x3-x4":
                self.assertEqual(pm.expectation_for(board)["chip"], "esp32s3",
                                 board)


class OrchestrationTest(unittest.TestCase):
    """run_matrix with fetch off and an injected boot: no Docker, no network.

    The synthetic assets are placed under a temporary scratch folder with the
    published filenames, so classify / merge / row building all run for real.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ybx-panel-matrix-test-")
        self.scratch = Path(self.tmp.name)
        self.assets = self.scratch / "assets"
        self.assets.mkdir(parents=True)
        head = bytearray(pm.APP_OFFSET)
        head[0] = pm.IMAGE_MAGIC
        head[pm.PARTITION_OFFSET:pm.PARTITION_OFFSET + 2] = pm.PARTITION_MAGIC
        self.head_bytes = bytes(head)

    def tearDown(self):
        self.tmp.cleanup()

    def write_asset(self, vendor, tag, board, data):
        path = self.assets / pm.asset_name(vendor, tag, board)
        path.write_bytes(data)
        return path

    def releases(self):
        return [{"vendor": "crosspoint", "repo": pm.CROSSPOINT_REPO,
                 "tag": "2.0.0rc", "boards": ("x4pro", "x4c", "picco")}]

    def test_merged_bytes_land_where_the_bootloader_looks(self):
        calls = []

        def fake_boot(image, **kwargs):
            calls.append((Path(image), kwargs))
            return {"frames": 3, "first_frame_sha256": "aa" * 32,
                    "last_frame_sha256": "bb" * 32, "first_frame_seconds": 9.0,
                    "wall_seconds": 14.0, "qemu_exit_status": None,
                    "qemu_log_tail": None, "error": None}

        for board in ("x4pro", "x4c", "picco"):
            self.write_asset("crosspoint", "2.0.0rc", board, app_image(0x900))
        rows = pm.run_matrix(self.scratch, releases=self.releases(),
                             boot=fake_boot, head=self.head_bytes,
                             fetch=False, log=lambda text: None)
        self.assertEqual([row["board"] for row in rows],
                         ["x4pro", "x4c", "picco"])
        self.assertEqual([row["verdict"] for row in rows], [pm.PAINTED] * 3)
        image = calls[0][0].read_bytes()
        self.assertEqual(len(image), pm.FLASH_SIZE)
        self.assertEqual(image[0:pm.APP_OFFSET], self.head_bytes)
        self.assertEqual(image[pm.APP_OFFSET:pm.APP_OFFSET + 0x900],
                         app_image(0x900))
        # every boot is pointed at the merged folder, not at the raw asset
        self.assertEqual(calls[0][1]["fw_dir"], self.scratch / "merged")

    def test_a_non_esp_asset_is_refused_without_a_boot_attempt(self):
        calls = []

        def fake_boot(image, **kwargs):
            calls.append(image)
            return {"frames": 0, "qemu_exit_status": None, "error": None}

        self.write_asset("crosspoint", "2.0.0rc", "x4pro", b"not-a-bin" * 60)
        rows = pm.run_matrix(self.scratch, releases=self.releases(),
                             boot=fake_boot, head=self.head_bytes,
                             boards=("x4pro",), fetch=False,
                             log=lambda text: None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], pm.REFUSED)
        self.assertEqual(calls, [])
        self.assertIsNone(rows[0]["first_frame_sha256"])

    def test_board_filter_and_report_shape(self):
        self.write_asset("crosspoint", "2.0.0rc", "x4c", app_image(0x400))
        rows = pm.run_matrix(self.scratch, releases=self.releases(),
                             boot=lambda image, **kw: {
                                 "frames": 0, "qemu_exit_status": None,
                                 "error": None},
                             head=self.head_bytes, boards=("x4c",),
                             fetch=False, log=lambda text: None)
        report = pm.build_report(rows, self.scratch, {"run_seconds": 90})
        self.assertEqual(report["schema"], "ybx-panel-matrix/1")
        self.assertEqual(report["machine"]["chip"], "esp32s3")
        self.assertEqual(report["summary"]["rows"], 1)
        self.assertEqual(report["summary"][pm.PAINTED_NOTHING], 1)
        rendered = pm.render_table(rows)
        self.assertIn("crosspoint/x4c", rendered)
        self.assertIn("painted nothing", rendered)


class MachineRowTest(unittest.TestCase):
    def test_machine_description_matches_the_emulated_panel(self):
        self.assertEqual(pm.X4PRO_MACHINE["chip"], "esp32s3")
        self.assertEqual(pm.X4PRO_MACHINE["epd_controller"], "SSD1677")
        self.assertIn("480x800", pm.X4PRO_MACHINE["panel"])
        self.assertEqual(pm.X4PRO_MACHINE["epd_pins"],
                         {"sclk": 12, "mosi": 11, "cs": 13, "dc": 18,
                          "rst": 14, "busy": 6})

    def test_x4pro_expectation_agrees_with_the_machine(self):
        expectation = pm.expectation_for("x4pro")
        pins = pm.X4PRO_MACHINE["epd_pins"]
        spelled = expectation["pins"].upper()
        for signal, gpio in pins.items():
            self.assertIn(f"{signal.upper()}{gpio}", spelled)


if __name__ == "__main__":
    unittest.main()
