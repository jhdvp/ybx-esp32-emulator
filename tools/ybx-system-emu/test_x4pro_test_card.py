"""Host tests for x4pro_test_card: the FAT16 builder is checked by parsing it
back, and every browse-gate threshold must either pin to a measured frame from
0084 or say PROVISIONAL.

No Docker, no network, no guest. The pristine QA image is only ever hashed; if
the artifacts folder is absent the write-protection test skips, like the other
firmware-dependent suites.
"""
import hashlib
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import x4pro_test_card as card

PRISTINE = Path(
    "~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/"
    "qa-20261029/ybx-test-sd-fat16-mbr-pristine.img").expanduser()


class TestCardBuilder(unittest.TestCase):
    def test_round_trip_matches_written_files(self):
        """read_fat16_tree() must return the exact digests we wrote."""
        with TemporaryDirectory() as td:
            dest = Path(td) / "card.img"
            record = card.make_test_card(dest)
            self.assertTrue(record["round_trip_ok"], record)
            listed = {
                path.lstrip("/"): info["sha256"]
                for path, info in card.read_fat16_tree(dest)["files"].items()
                if not path.endswith("/")
            }
            expected = {
                name: hashlib.sha256(blob).hexdigest()
                for name, blob in card.test_library_files()
                if not name.endswith("/")
            }
            self.assertEqual(listed, expected)

    def test_volume_shape_mirrors_the_pristine_card(self):
        """The guest's SdFat path must see the geometry 0084 measured."""
        with TemporaryDirectory() as td:
            dest = Path(td) / "card.img"
            card.make_test_card(dest)
            bpb = dest.read_bytes()[card.PART_START * card.SECTOR:][:64]
            self.assertEqual(card.PART_START, 2048)
            self.assertEqual(struct.unpack_from("<H", bpb, 11)[0], 512)
            self.assertEqual(bpb[13], card.SPC)
            self.assertEqual(struct.unpack_from("<H", bpb, 14)[0], card.RESERVED)
            self.assertEqual(bpb[16], 2)
            self.assertEqual(struct.unpack_from("<H", bpb, 17)[0], card.ROOT_ENTRIES)
            self.assertEqual(struct.unpack_from("<H", bpb, 22)[0], card.FAT_SECTORS)
            self.assertEqual(bpb[43:43 + len(card.VOLUME_LABEL)], card.VOLUME_LABEL)
            self.assertEqual(dest.read_bytes()[510:512], b"\x55\xaa")

    @unittest.skipUnless(PRISTINE.exists(), "pristine QA image not on this host")
    def test_builder_never_touches_pristine(self):
        before = hashlib.sha256(PRISTINE.read_bytes()).hexdigest()
        with TemporaryDirectory() as td:
            card.make_test_card(Path(td) / "card.img")
        self.assertEqual(hashlib.sha256(PRISTINE.read_bytes()).hexdigest(), before)
        verdict = card.verify_builder_does_not_touch_pristine(PRISTINE)
        self.assertTrue(verdict["builder_never_writes_pristine"], verdict)
        self.assertEqual(verdict["pristine_sha256"], before)


class TestVfatLongNames(unittest.TestCase):
    """Independent FAT/VFAT invariants: the parser alone used to miss these."""

    def test_checksum_is_eight_bit_and_at_byte_13(self):
        short = b"FIELD_~1   "
        expected = 0
        for byte in short:
            expected = (((expected & 1) << 7) | (expected >> 1))
            expected = (expected + byte) % 256
        self.assertEqual(card._checksum(short), expected)
        self.assertRaises(ValueError, card._checksum, b"bad")
        slots = card._lfn_records("Field Notes", short)
        self.assertEqual(len(slots), 32)
        self.assertEqual(slots[13], expected)
        self.assertEqual(slots[26:28], b"\x00\x00")
        self.assertEqual(slots[11], 0x0f)
        self.assertEqual(slots[12], 0)
        self.assertEqual(slots[0], 0x41)

    def test_exact_thirteen_utf16_units_uses_one_slot(self):
        short = b"LONGNA~1TXT"
        for name, expected_count in (("abcdefghijklm", 1),
                                     ("abcdefghijklmno", 2),
                                     ("Book📘.epub", 1)):
            with self.subTest(name=name):
                data = card._lfn_records(name, short)
                self.assertEqual(len(data), expected_count * 32)
                self.assertEqual(data[0] & 0x40, 0x40)
                for i in range(0, len(data), 32):
                    self.assertEqual(data[i+11], 0x0f)
                    self.assertEqual(data[i+13], card._checksum(short))
                    self.assertEqual(data[i+26:i+28], b"\x00\x00")

    def test_guest_card_matches_full_pristine_geometry(self):
        self.assertEqual(len(card.wrap_mbr(card.build_fat16([]))), 134217728)


class TestBrowseGates(unittest.TestCase):
    def test_status_reports_every_gate(self):
        self.assertEqual(set(card.browse_gate_status()),
                         {"bootsd_writes", "empty_card_lib", "paired_card_lib",
                          "browse_listed", "open_epub"})

    def test_measured_gates_pin_a_frame_and_provisional_say_so(self):
        for name, gate in card.browse_gate_status().items():
            with self.subTest(gate=name):
                if gate.get("provisional", False):
                    self.assertIn("PROVISIONAL", gate["value"])
                else:
                    self.assertIn("(from frame", gate["value"])

    def test_thresholds_are_the_numbers_0084_measured(self):
        """0084 §5.1/§5.2/§5.3/§6: changed-sector counts per guest action."""
        self.assertEqual(card.BOOT_ONLY_SD_WRITES, 0)         # §5.1 boot only
        self.assertEqual(card.EMPTY_CARD_LIBRARY_SECTORS, 5)  # §5.2
        self.assertEqual(card.PAIRED_CARD_LIBRARY_SECTORS, 14)  # §5.3
        self.assertEqual(card.OPEN_EPUB_SECTORS, 33)          # §6
        self.assertIsNone(card.BROWSE_FILES_LISTED)   # not measured in 0084


if __name__ == "__main__":
    unittest.main()
