"""Host-only tests for probe_x4pro_sd_content: FAT16 parsing and write diff.

Builders write small MBR-wrapped FAT16 images into a temporary directory; the
probe under test only ever reads them. No Docker, network or guest involved.
"""
import hashlib
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import probe_x4pro_sd_content as probe

SECTOR = 512
PART_LBA = 1
PART_SECTORS = 5000
RESERVED = 1
NUM_FATS = 2
SECTORS_PER_FAT = 20
ROOT_ENTRIES = 64
ROOT_SECTORS = ROOT_ENTRIES * 32 // SECTOR
CLUSTER_COUNT = PART_SECTORS - RESERVED - NUM_FATS * SECTORS_PER_FAT - ROOT_SECTORS
DATA_LBA = PART_LBA + RESERVED + NUM_FATS * SECTORS_PER_FAT + ROOT_SECTORS

HELLO = {"path": ("HELLO.TXT",), "data": b"hello sd\n",
         "short": b"HELLO   TXT", "lfn": False}
BOOK = {"path": ("BOOKS", "The Little Book.epub"),
        "data": bytes(range(256)) * 12,
        "short": b"TINY~1  EPU", "lfn": True}
DEFAULT_FILES = [HELLO, BOOK]


def short_name(name):
    base, _, extension = name.partition(".")
    return (base.upper().ljust(8)[:8] + extension.upper().ljust(3)[:3]).encode()


def lfn_checksum(short11):
    total = 0
    for byte in short11:
        total = (((total & 1) << 7) | (total >> 1)) + byte
        total &= 0xFF
    return total


def lfn_records(long_name, short11):
    units = long_name.encode("utf-16-le")
    codepoints = [units[i] | (units[i + 1] << 8)
                  for i in range(0, len(units), 2)]
    codepoints.append(0x0000)
    while len(codepoints) % 13:
        codepoints.append(0xFFFF)
    chunks = [codepoints[i:i + 13] for i in range(0, len(codepoints), 13)]
    checksum = lfn_checksum(short11)
    records = []
    for sequence in range(len(chunks), 0, -1):
        raw = bytearray(32)
        raw[0] = sequence | (0x40 if sequence == len(chunks) else 0)
        raw[11] = 0x0F
        raw[13] = checksum
        chars = b"".join(struct.pack("<H", value) for value in chunks[sequence - 1])
        raw[1:11] = chars[:10]
        raw[14:26] = chars[10:22]
        raw[28:32] = chars[22:26]
        records.append(bytes(raw))
    return b"".join(records)


def dir_entry(short11, attributes, cluster, size):
    raw = bytearray(32)
    raw[:11] = short11
    raw[11] = attributes
    struct.pack_into("<H", raw, 20, cluster >> 16)
    struct.pack_into("<H", raw, 26, cluster & 0xFFFF)
    struct.pack_into("<I", raw, 28, size)
    return bytes(raw)


def build_image(path, files=None, with_mbr=True, part_type=0x06):
    files = [dict(item) for item in (files if files is not None else DEFAULT_FILES)]
    for item in files:
        item["path"] = tuple(item["path"])
        item["short"] = item.get("short") or short_name(item["path"][-1])
        item.setdefault("lfn", False)
        item.setdefault("data", b"")
    files.sort(key=lambda item: item["path"])
    directories = sorted({item["path"][:length]
                          for item in files
                          for length in range(1, len(item["path"]))},
                         key=lambda item: (len(item), item))
    next_cluster = 2
    allocation = {}

    def allocate(count):
        nonlocal next_cluster
        chain = list(range(next_cluster, next_cluster + count))
        next_cluster += count
        return chain

    for directory in directories:
        allocation[directory] = allocate(1)
    for item in files:
        clusters = (len(item["data"]) + SECTOR - 1) // SECTOR
        allocation[item["path"]] = allocate(clusters) if clusters else []
    fat = [0] * (SECTORS_PER_FAT * SECTOR // 2)
    fat[0] = 0xFFF8
    fat[1] = 0xFFFF
    for chain in allocation.values():
        for current, following in zip(chain, chain[1:]):
            fat[current] = following
        if chain:
            fat[chain[-1]] = 0xFFFF

    def children_of(parent):
        items = [{"path": directory, "is_dir": True}
                 for directory in directories if directory[:-1] == parent]
        items += [{"path": item["path"], "is_dir": False, "item": item}
                  for item in files if item["path"][:-1] == parent]
        return sorted(items, key=lambda child: child["path"][-1])

    def child_entry(child):
        if child["is_dir"]:
            return dir_entry(short_name(child["path"][-1]), 0x10,
                             allocation[child["path"]][0], 0)
        item = child["item"]
        entry = allocation[item["path"]]
        records = b""
        if item["lfn"]:
            records = lfn_records(item["path"][-1], item["short"])
        return records + dir_entry(item["short"], 0x20,
                                   entry[0] if entry else 0, len(item["data"]))

    def directory_data(directory):
        parent_chain = allocation.get(directory) or [0]
        grandparent = directory[:-1] if directory else ()
        grandparent_chain = allocation.get(grandparent) or [0]
        records = [dir_entry(b"." + b" " * 10, 0x10, parent_chain[0], 0),
                   dir_entry(b".." + b" " * 9, 0x10, grandparent_chain[0], 0)]
        for child in children_of(directory):
            records.append(child_entry(child))
        return b"".join(records).ljust(SECTOR, b"\x00")

    base_lba = PART_LBA if with_mbr else 0
    image = bytearray((base_lba + PART_SECTORS) * SECTOR)
    if with_mbr:
        image[446] = 0x00
        image[446 + 4] = part_type
        struct.pack_into("<I", image, 446 + 8, PART_LBA)
        struct.pack_into("<I", image, 446 + 12, PART_SECTORS)
        image[510:512] = b"\x55\xaa"
    boot = bytearray(SECTOR)
    boot[0:3] = b"\xeb\x3c\x90"
    boot[3:11] = b"mkfs.fat"
    struct.pack_into("<H", boot, 11, SECTOR)
    boot[13] = 1
    struct.pack_into("<H", boot, 14, RESERVED)
    boot[16] = NUM_FATS
    struct.pack_into("<H", boot, 17, ROOT_ENTRIES)
    boot[21] = 0xF8
    struct.pack_into("<H", boot, 22, SECTORS_PER_FAT)
    struct.pack_into("<H", boot, 24, 63)
    struct.pack_into("<H", boot, 26, 64)
    struct.pack_into("<I", boot, 28, base_lba)
    struct.pack_into("<I", boot, 32, PART_SECTORS)
    boot[38] = 0x29
    struct.pack_into("<I", boot, 39, 0xDEADBEEF)
    boot[43:54] = b"YBXTEST    "
    boot[54:62] = b"FAT16   "
    boot[510:512] = b"\x55\xaa"
    boot_lba = base_lba
    image[boot_lba * SECTOR:(boot_lba + 1) * SECTOR] = boot
    fat_bytes = b"".join(struct.pack("<H", value) for value in fat)
    for index in range(NUM_FATS):
        start = (base_lba + RESERVED + index * SECTORS_PER_FAT) * SECTOR
        image[start:start + len(fat_bytes)] = fat_bytes
    root_lba = base_lba + RESERVED + NUM_FATS * SECTORS_PER_FAT
    root = bytearray(dir_entry(b"YBXTEST    ", 0x08, 0, 0))
    for child in children_of(()):
        root += child_entry(child)
    image[root_lba * SECTOR:root_lba * SECTOR + len(root)] = root
    data_lba = root_lba + ROOT_SECTORS
    for directory in directories:
        cluster = allocation[directory][0]
        start = (data_lba + (cluster - 2)) * SECTOR
        image[start:start + SECTOR] = directory_data(directory)
    for item in files:
        for index, cluster in enumerate(allocation[item["path"]]):
            start = (data_lba + (cluster - 2)) * SECTOR
            chunk = item["data"][index * SECTOR:(index + 1) * SECTOR]
            image[start:start + len(chunk)] = chunk
    Path(path).write_bytes(image)
    return {"allocation": allocation, "next_cluster": next_cluster}


class SdContentProbeTests(unittest.TestCase):
    def test_layout_free_space_and_tree(self):
        with TemporaryDirectory() as temp:
            image = Path(temp) / "pristine.img"
            build_image(image)
            summary, _ = probe.inspect_image(image)
        self.assertEqual(summary["mbr"]["layout"], "mbr")
        self.assertEqual(summary["mbr"]["partition_lba"], PART_LBA)
        self.assertEqual(summary["mbr"]["partition_type"], 0x06)
        fat = summary["fat16"]
        self.assertEqual(fat["fat_lba"], 2)
        self.assertEqual(fat["root_lba"], 42)
        self.assertEqual(fat["data_lba"], DATA_LBA)
        self.assertEqual(fat["cluster_count"], CLUSTER_COUNT)
        self.assertEqual(fat["cluster_bytes"], 512)
        self.assertEqual(fat["used_clusters"], 8)
        self.assertEqual(fat["free_clusters"], CLUSTER_COUNT - 8)
        self.assertEqual(fat["free_bytes"], (CLUSTER_COUNT - 8) * 512)
        self.assertEqual(fat["bad_clusters"], 0)
        self.assertEqual(fat["volume_label_entry"], "YBXTEST")
        self.assertEqual(fat["bpb_volume_label"], "YBXTEST")
        self.assertTrue(fat["fat_copies_identical"])
        self.assertTrue(fat["bpb_hidden_matches_partition_lba"])
        self.assertEqual([entry["name"] for entry in summary["root_entries"]],
                         ["YBXTEST", "BOOKS", "HELLO.TXT"])
        nodes = {node["path"]: node for node in summary["tree"]}
        self.assertEqual(sorted(nodes), ["/BOOKS", "/BOOKS/The Little Book.epub",
                                         "/HELLO.TXT"])
        self.assertEqual(nodes["/BOOKS"]["type"], "dir")
        self.assertEqual(nodes["/BOOKS"]["entries"], 1)
        self.assertEqual(nodes["/HELLO.TXT"]["content_sha256"],
                         hashlib.sha256(b"hello sd\n").hexdigest())
        self.assertEqual(nodes["/BOOKS/The Little Book.epub"]["content_sha256"],
                         hashlib.sha256(bytes(range(256)) * 12).hexdigest())
        self.assertFalse(summary["tree_truncated"])

    def test_long_name_and_short_name(self):
        with TemporaryDirectory() as temp:
            image = Path(temp) / "pristine.img"
            build_image(image)
            summary, _ = probe.inspect_image(image)
        node = next(node for node in summary["tree"]
                    if node["path"] == "/BOOKS/The Little Book.epub")
        self.assertEqual(node["long_name"], "The Little Book.epub")
        self.assertEqual(node["short_name"], "TINY~1.EPU")

    def test_identical_copies_have_no_diff(self):
        with TemporaryDirectory() as temp:
            pristine = Path(temp) / "pristine.img"
            guest = Path(temp) / "guest.img"
            build_image(pristine)
            guest.write_bytes(pristine.read_bytes())
            report, _, _ = probe.analyze(pristine, guest)
        diff = report["diff"]
        self.assertTrue(diff["images_identical"])
        self.assertFalse(diff["guest_wrote_to_sd"])
        self.assertEqual(diff["changed_sectors"]["sectors"], 0)
        self.assertFalse(diff["mbr_changed"])
        self.assertFalse(diff["boot_sector_changed"])
        self.assertEqual(diff["tree"]["added"], [])
        self.assertEqual(diff["tree"]["removed"], [])
        self.assertEqual(diff["tree"]["changed"], [])
        self.assertEqual(diff["fat"]["allocated_clusters"], 0)
        self.assertEqual(diff["free_space"]["free_bytes_delta"], 0)

    def test_guest_adds_file_allocates_clusters_and_extracts(self):
        added = {"path": ("NOTES.BIN",), "data": b"\xab" * 4096,
                 "short": b"NOTES   BIN", "lfn": False}
        with TemporaryDirectory() as temp:
            pristine = Path(temp) / "pristine.img"
            guest = Path(temp) / "guest.img"
            build_image(pristine)
            build_image(guest, DEFAULT_FILES + [added])
            report, _, guest_context = probe.analyze(pristine, guest)
            out = Path(temp) / "extract"
            extracted = probe.extract_changed(guest, guest_context,
                                              report["diff"], out, 1 << 20)
            with self.assertRaises(FileExistsError):
                probe.extract_changed(guest, guest_context, report["diff"],
                                      out, 1 << 20)
            payload = (out / "NOTES.BIN").read_bytes()
        diff = report["diff"]
        self.assertTrue(diff["guest_wrote_to_sd"])
        self.assertEqual([node["path"] for node in diff["tree"]["added"]],
                         ["/NOTES.BIN"])
        self.assertEqual(diff["guest_created_entries"], 1)
        self.assertEqual([entry["short_name"]
                          for entry in diff["root_entries"]["added"]],
                         ["NOTES.BIN"])
        self.assertEqual(diff["fat"]["allocated_clusters"], 8)
        self.assertEqual(diff["free_space"]["free_bytes_delta"], -4096)
        self.assertGreater(diff["changed_sectors"]["sectors"], 0)
        self.assertTrue(diff["changed_sectors"]["ranges"])
        self.assertFalse(diff["mbr_changed"])
        self.assertEqual(payload, b"\xab" * 4096)
        self.assertEqual(extracted[0]["bytes"], 4096)
        self.assertEqual(extracted[0]["sha256"],
                         hashlib.sha256(b"\xab" * 4096).hexdigest())

    def test_guest_changes_file_content_in_place(self):
        changed = dict(HELLO, data=b"HELLO SD\n")
        with TemporaryDirectory() as temp:
            pristine = Path(temp) / "pristine.img"
            guest = Path(temp) / "guest.img"
            build_image(pristine)
            build_image(guest, [changed, BOOK])
            report, _, _ = probe.analyze(pristine, guest)
        diff = report["diff"]
        self.assertEqual([entry["path"] for entry in diff["tree"]["changed"]],
                         ["/HELLO.TXT"])
        self.assertEqual(diff["tree"]["added"], [])
        self.assertEqual(diff["tree"]["removed"], [])
        self.assertEqual(diff["fat"]["allocated_clusters"], 0)
        self.assertEqual(diff["changed_sectors"]["sectors"], 1)
        self.assertGreater(diff["changed_sectors"]["ranges"][0]["lba"],
                           DATA_LBA)

    def test_parse_dir_entries_skips_deleted_and_stops_at_zero(self):
        deleted = bytearray(dir_entry(b"GONE    TXT", 0x20, 2, 1))
        deleted[0] = 0xE5
        valid = dir_entry(b"KEEP    TXT", 0x20, 3, 4)
        trailing = dir_entry(b"AFTER   TXT", 0x20, 4, 5)
        entries = probe.parse_dir_entries(
            bytes(deleted) + valid + bytes(32) + trailing)
        self.assertEqual([entry["short_name"] for entry in entries], ["KEEP.TXT"])

    def test_nt_case_flags_lowercase_display_name(self):
        raw = bytearray(dir_entry(b"NOTES   TXT", 0x20, 2, 37))
        raw[12] = 0x18
        entries = probe.parse_dir_entries(bytes(raw) + bytes(32))
        self.assertEqual(entries[0]["short_name"], "NOTES.TXT")
        self.assertEqual(entries[0]["name"], "notes.txt")
        self.assertIsNone(entries[0]["long_name"])

    def test_reject_non_fat16_partition(self):
        with TemporaryDirectory() as temp:
            image = Path(temp) / "fat32.img"
            build_image(image, part_type=0x0C)
            with self.assertRaisesRegex(ValueError, "no FAT16 partition"):
                probe.inspect_image(image)

    def test_reject_cyclic_cluster_chain(self):
        with TemporaryDirectory() as temp:
            image = Path(temp) / "cyclic.img"
            build_image(image, [HELLO])
            data = bytearray(image.read_bytes())
            cluster = 2  # first allocated cluster in a one-file image
            struct.pack_into("<H", data,
                             (PART_LBA + RESERVED) * SECTOR + cluster * 2,
                             cluster)
            image.write_bytes(data)
            with self.assertRaisesRegex(ValueError, "cyclic FAT chain"):
                probe.inspect_image(image)

    def test_superfloppy_without_mbr(self):
        with TemporaryDirectory() as temp:
            image = Path(temp) / "superfloppy.img"
            build_image(image, with_mbr=False)
            summary, _ = probe.inspect_image(image)
        self.assertEqual(summary["mbr"]["layout"], "superfloppy")
        self.assertEqual(summary["mbr"]["partition_lba"], 0)
        self.assertEqual(summary["fat16"]["fat_lba"], 1)
        self.assertEqual(summary["fat16"]["root_lba"], 41)
        self.assertEqual(summary["fat16"]["data_lba"], 45)
        self.assertEqual(summary["fat16"]["cluster_count"], CLUSTER_COUNT)
        self.assertTrue(summary["fat16"]["bpb_hidden_matches_partition_lba"])


if __name__ == "__main__":
    unittest.main()
