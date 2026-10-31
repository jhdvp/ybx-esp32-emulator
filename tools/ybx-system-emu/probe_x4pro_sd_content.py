#!/usr/bin/env python3
"""Read-only FAT16/MBR SD card probe with a guest-write diff.

Reads the pristine X4 Pro card image and the guest's card copy after a QEMU
run, then reports the FAT16 layout (MBR, BPB, root tree, free space) and
exactly what changed: sectors, FAT entries, directory entries and file
contents. Inputs are opened read-only; optional extraction writes only below
--extract-dir and never overwrites an existing file.

A host-side image diff can prove guest WRITES and the resulting on-card FAT
structures. It cannot prove that the guest read a file, mounted the volume or
painted a UI: those need guest-side evidence (UART, frames, navigation).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

SECTOR = 512
FAT16_MIN_CLUSTERS = 4085
FAT16_MAX_CLUSTERS = 65524
FAT_END_OF_CHAIN = 0xFFF8
FAT_BAD_CLUSTER = 0xFFF7
DIFF_CHUNK_BYTES = 1 << 20
MAX_DIFF_RANGES = 64
MAX_FAT_CHANGE_SAMPLES = 64
DEFAULT_HASH_LIMIT = 16 * 1024 * 1024
DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_ENTRIES = 20000


def _u16(buf, offset):
    return struct.unpack_from("<H", buf, offset)[0]


def _u32(buf, offset):
    return struct.unpack_from("<I", buf, offset)[0]


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _first_sector(path):
    with path.open("rb") as handle:
        return handle.read(SECTOR)


def read_sectors(handle, lba, count):
    handle.seek(lba * SECTOR)
    data = handle.read(count * SECTOR)
    if len(data) != count * SECTOR:
        raise ValueError(f"truncated image: LBA {lba} +{count} sectors")
    return data


def parse_mbr(handle, size_bytes):
    """Locate the FAT16 partition: MBR entry #1..4 or a superfloppy boot sector."""
    head = read_sectors(handle, 0, 1)
    if head[510:512] != b"\x55\xaa":
        raise ValueError("image has no MBR/boot signature 0x55AA at LBA0")
    partitions = []
    selected = None
    for index in range(4):
        offset = 446 + index * 16
        status = head[offset]
        part_type = head[offset + 4]
        lba = _u32(head, offset + 8)
        sectors = _u32(head, offset + 12)
        if part_type == 0 and sectors == 0:
            continue
        entry = {"index": index, "status": status, "type": part_type,
                 "lba": lba, "sectors": sectors}
        partitions.append(entry)
        if (selected is None and status in (0x00, 0x80)
                and part_type in (0x04, 0x06, 0x0E)
                and lba > 0 and sectors > 0
                and (lba + sectors) * SECTOR <= size_bytes):
            selected = entry
    if selected is not None:
        return {"layout": "mbr", "signature": head[510:512].hex(),
                "partitions": partitions,
                "partition_index": selected["index"],
                "partition_lba": selected["lba"],
                "partition_sectors": selected["sectors"],
                "partition_type": selected["type"]}
    # mkfs.fat images without an MBR start the FAT volume at LBA0.
    if head[54:59] in (b"FAT16", b"FAT12", b"FAT32"):
        return {"layout": "superfloppy", "signature": head[510:512].hex(),
                "partitions": partitions, "partition_index": 0,
                "partition_lba": 0,
                "partition_sectors": size_bytes // SECTOR,
                "partition_type": 0}
    raise ValueError("no FAT16 partition entry (types 0x04/0x06/0x0E) at LBA0")


def parse_fat16(handle, mbr):
    """Parse and validate a FAT16 BPB; returns the on-card geometry."""
    boot = read_sectors(handle, mbr["partition_lba"], 1)
    if boot[510:512] != b"\x55\xaa":
        raise ValueError("FAT boot sector has no 0x55AA signature")
    bytes_per_sector = _u16(boot, 11)
    sectors_per_cluster = boot[13]
    reserved = _u16(boot, 14)
    num_fats = boot[16]
    root_entries = _u16(boot, 17)
    total16 = _u16(boot, 19)
    media = boot[21]
    sectors_per_fat = _u16(boot, 22)
    hidden = _u32(boot, 28)
    total32 = _u32(boot, 32)
    fs_type = boot[54:62].decode("ascii", "replace").rstrip("\x00 ")
    volume_label = boot[43:54].decode("ascii", "replace").rstrip(" \x00")
    if bytes_per_sector != SECTOR:
        raise ValueError(f"only 512-byte FAT sectors are supported, got "
                         f"{bytes_per_sector}")
    if (sectors_per_cluster <= 0 or sectors_per_cluster > 128
            or sectors_per_cluster & (sectors_per_cluster - 1)):
        raise ValueError(f"invalid sectors per cluster {sectors_per_cluster}")
    if not reserved or not num_fats or not sectors_per_fat or not root_entries:
        raise ValueError("invalid FAT16 BPB geometry")
    total_sectors = total16 or total32
    if not total_sectors:
        raise ValueError("invalid FAT16 total sector count")
    root_dir_sectors = (root_entries * 32 + SECTOR - 1) // SECTOR
    fat_lba = mbr["partition_lba"] + reserved
    root_lba = fat_lba + num_fats * sectors_per_fat
    data_lba = root_lba + root_dir_sectors
    if mbr["layout"] == "mbr" and total_sectors > mbr["partition_sectors"]:
        raise ValueError("FAT16 BPB exceeds its MBR partition")
    data_sectors = total_sectors - reserved - num_fats * sectors_per_fat - root_dir_sectors
    if data_sectors <= 0:
        raise ValueError("FAT16 BPB has no data area")
    cluster_count = data_sectors // sectors_per_cluster
    if not (FAT16_MIN_CLUSTERS <= cluster_count <= FAT16_MAX_CLUSTERS):
        raise ValueError(f"not a FAT16 volume: {cluster_count} clusters "
                         f"(valid {FAT16_MIN_CLUSTERS}..{FAT16_MAX_CLUSTERS})")
    fat_entries_available = sectors_per_fat * SECTOR // 2
    if fat_entries_available < cluster_count + 2:
        raise ValueError(f"FAT too small: {fat_entries_available} entries for "
                         f"{cluster_count + 2} clusters")
    return {
        "oem": boot[3:11].decode("ascii", "replace"),
        "bytes_per_sector": bytes_per_sector,
        "sectors_per_cluster": sectors_per_cluster,
        "cluster_bytes": sectors_per_cluster * bytes_per_sector,
        "reserved_sectors": reserved,
        "num_fats": num_fats,
        "root_entries": root_entries,
        "root_dir_sectors": root_dir_sectors,
        "total_sectors": total_sectors,
        "media": f"0x{media:02x}",
        "sectors_per_fat": sectors_per_fat,
        "hidden_sectors": hidden,
        "fs_type": fs_type,
        "bpb_volume_label": volume_label,
        "volume_id": f"0x{_u32(boot, 39):08x}",
        "partition_lba": mbr["partition_lba"],
        "fat_lba": fat_lba,
        "root_lba": root_lba,
        "root_dir_sectors": root_dir_sectors,
        "data_lba": data_lba,
        "cluster_count": cluster_count,
        "fat_entries_available": fat_entries_available,
        "bpb_hidden_matches_partition_lba": (
            hidden == mbr["partition_lba"]),
    }


def read_fat(handle, geometry):
    """Return FAT #1 and whether every FAT copy is byte-identical."""
    copies = [read_sectors(handle, geometry["fat_lba"] + index *
                           geometry["sectors_per_fat"],
                           geometry["sectors_per_fat"])
              for index in range(geometry["num_fats"])]
    fat = copies[0]
    entries = list(struct.unpack(f"<{len(fat) // 2}H", fat))
    return entries, all(copy == fat for copy in copies[1:])


def decode_dos_datetime(date, time):
    if not date:
        return None
    day = date & 0x1F
    month = (date >> 5) & 0x0F
    year = 1980 + ((date >> 9) & 0x7F)
    hour = (time >> 11) & 0x1F
    minute = (time >> 5) & 0x3F
    second = (time & 0x1F) * 2
    if not (1 <= day <= 31 and 1 <= month <= 12 and hour < 24
            and minute < 60 and second < 60):
        return None
    return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}"


def format_short_name(raw):
    base = bytearray(raw[:8])
    if base[0] == 0x05:
        base[0] = 0xE5
    base_text = bytes(base).rstrip(b" ").decode("ascii", "replace")
    ext = raw[8:11].rstrip(b" ").decode("ascii", "replace")
    return base_text + ("." + ext if ext else "")


def display_short_name(raw):
    """8.3 name as shown by the guest: NT case flags lower-case base/ext."""
    text = format_short_name(raw)
    flags = raw[12]
    if not text or not flags & 0x18:
        return text
    base, dot, ext = text.partition(".")
    if flags & 0x08:
        base = base.lower()
    if flags & 0x10:
        ext = ext.lower()
    return base + (dot + ext if dot else "")


def _lfn_text(raw):
    units = raw[1:11] + raw[14:26] + raw[28:32]
    text = units.decode("utf-16-le", "replace")
    text = text.split("\x00", 1)[0]
    return text.replace("\uffff", "")


def attribute_flags(value):
    flags = []
    for bit, name in ((0x01, "read_only"), (0x02, "hidden"), (0x04, "system"),
                      (0x08, "volume_label"), (0x10, "directory"),
                      (0x20, "archive")):
        if value & bit:
            flags.append(name)
    return flags


def parse_dir_entries(data):
    """Parse 32-byte FAT directory records, including long file names."""
    entries = []
    lfn_chunks = {}
    for offset in range(0, len(data) - 31, 32):
        raw = data[offset:offset + 32]
        if raw[0] == 0x00:
            break
        if raw[0] == 0xE5:
            lfn_chunks = {}
            continue
        attributes = raw[11]
        if attributes == 0x0F:
            sequence = raw[0] & 0x1F
            if raw[0] & 0x40:
                lfn_chunks = {}
            lfn_chunks[sequence] = _lfn_text(raw)
            continue
        long_name = None
        if lfn_chunks:
            long_name = "".join(lfn_chunks.get(index, "")
                                for index in range(1, max(lfn_chunks) + 1))
            long_name = long_name.split("\x00", 1)[0]
            lfn_chunks = {}
        short_name = format_short_name(raw)
        entries.append({
            "short_name": short_name,
            "name": long_name or display_short_name(raw),
            "long_name": long_name,
            "attributes": attributes,
            "attribute_flags": attribute_flags(attributes),
            "is_dir": bool(attributes & 0x10),
            "is_volume_label": bool(attributes & 0x08),
            "first_cluster": _u16(raw, 26),
            "size": _u32(raw, 28),
            "modified": decode_dos_datetime(_u16(raw, 24), _u16(raw, 22)),
        })
    return entries


def cluster_chain(fat, first_cluster):
    if first_cluster < 2 or first_cluster >= len(fat):
        raise ValueError(f"invalid first cluster {first_cluster}")
    chain = []
    seen = set()
    cluster = first_cluster
    while True:
        if cluster in seen:
            raise ValueError(f"cyclic FAT chain at cluster {cluster}")
        if cluster < 2 or cluster >= len(fat):
            raise ValueError(f"FAT chain leaves the data area at {cluster}")
        if cluster == FAT_BAD_CLUSTER:
            raise ValueError(f"FAT chain hits a bad cluster at {cluster}")
        if cluster >= FAT_END_OF_CHAIN:
            raise ValueError(f"FAT chain reaches an end marker at {cluster}")
        seen.add(cluster)
        chain.append(cluster)
        cluster = fat[cluster]
        if cluster >= FAT_END_OF_CHAIN:
            return chain
        if cluster == 0:
            raise ValueError("FAT chain ends in a free cluster")


def read_clusters(handle, geometry, clusters):
    per = geometry["sectors_per_cluster"]
    return b"".join(
        read_sectors(handle, geometry["data_lba"] + (cluster - 2) * per, per)
        for cluster in clusters)


def read_file(handle, geometry, fat, first_cluster, size):
    if size == 0:
        return b"", []
    needed = (size + geometry["cluster_bytes"] - 1) // geometry["cluster_bytes"]
    chain = cluster_chain(fat, first_cluster)[:needed]
    if len(chain) < needed:
        raise ValueError("FAT chain shorter than the file size")
    return read_clusters(handle, geometry, chain)[:size], chain


def free_space(fat, cluster_count):
    free = used = bad = 0
    for cluster in range(2, cluster_count + 2):
        value = fat[cluster]
        if value == 0:
            free += 1
        elif value == FAT_BAD_CLUSTER:
            bad += 1
        else:
            used += 1
    return free, used, bad


def walk_tree(handle, geometry, fat, hash_limit, max_depth, max_entries):
    """Walk the root directory recursively into flat path-keyed nodes."""
    root_data = read_sectors(handle, geometry["root_lba"],
                             geometry["root_dir_sectors"])
    root_entries = parse_dir_entries(root_data)
    nodes = []
    state = {"truncated": False}

    def directory_entries(entry):
        if entry["first_cluster"] == 0:
            return []
        if entry["first_cluster"] < 2 or entry["first_cluster"] >= geometry["cluster_count"] + 2:
            raise ValueError(f"{entry['name']}: directory cluster "
                             f"{entry['first_cluster']} outside the data area")
        chain = cluster_chain(fat, entry["first_cluster"])
        data = read_clusters(handle, geometry, chain)
        return parse_dir_entries(data)

    def walk(entries, prefix, depth):
        for entry in entries:
            if state["truncated"] or entry["is_volume_label"]:
                continue
            if entry["name"] in (".", ".."):
                continue
            if len(nodes) >= max_entries:
                state["truncated"] = True
                return
            path = prefix + entry["name"]
            node = {
                "path": path,
                "short_name": entry["short_name"],
                "long_name": entry["long_name"],
                "type": "dir" if entry["is_dir"] else "file",
                "attribute_flags": entry["attribute_flags"],
                "size": entry["size"],
                "first_cluster": entry["first_cluster"],
                "modified": entry["modified"],
            }
            if entry["is_dir"]:
                children = directory_entries(entry)
                node["entries"] = sum(1 for child in children
                                      if not child["is_volume_label"]
                                      and child["name"] not in (".", ".."))
                nodes.append(node)
                if depth >= max_depth:
                    state["truncated"] = True
                    continue
                walk(children, path + "/", depth + 1)
            else:
                if entry["size"] <= hash_limit:
                    content, chain = read_file(handle, geometry, fat,
                                               entry["first_cluster"],
                                               entry["size"])
                    node["content_sha256"] = hashlib.sha256(content).hexdigest()
                    node["clusters"] = len(chain)
                else:
                    node["content_sha256"] = None
                    node["clusters"] = None
                nodes.append(node)

    walk(root_entries, "/", 0)
    nodes.sort(key=lambda item: item["path"])
    return root_entries, nodes, state["truncated"]


def inspect_image(path, hash_limit=DEFAULT_HASH_LIMIT,
                  max_depth=DEFAULT_MAX_DEPTH, max_entries=DEFAULT_MAX_ENTRIES):
    """Read-only inspection; returns (JSON summary, internal context)."""
    size_bytes = path.stat().st_size
    if size_bytes < SECTOR or size_bytes % SECTOR:
        raise ValueError(f"{path}: size {size_bytes} is not whole 512-byte sectors")
    with path.open("rb") as handle:
        mbr = parse_mbr(handle, size_bytes)
        geometry = parse_fat16(handle, mbr)
        fat, fat_copies_identical = read_fat(handle, geometry)
        root_entries, nodes, truncated = walk_tree(
            handle, geometry, fat, hash_limit, max_depth, max_entries)
    free, used, bad = free_space(fat, geometry["cluster_count"])
    volume_entry = next((entry for entry in root_entries
                         if entry["is_volume_label"]), None)
    fat_bytes = geometry["sectors_per_fat"] * SECTOR
    summary = {
        "path": str(path.resolve()),
        "size_bytes": size_bytes,
        "size_mib": round(size_bytes / (1 << 20), 3),
        "sha256": _sha256_file(path),
        "read_only": True,
        "mbr": mbr,
        "fat16": {
            **geometry,
            "free_clusters": free,
            "used_clusters": used,
            "bad_clusters": bad,
            "free_bytes": free * geometry["cluster_bytes"],
            "used_bytes": used * geometry["cluster_bytes"],
            "free_mib": round(free * geometry["cluster_bytes"] / (1 << 20), 3),
            "volume_label_entry": volume_entry["name"] if volume_entry else None,
            "fat_copies_identical": fat_copies_identical,
            "fat_sha256": hashlib.sha256(
                b"".join(value.to_bytes(2, "little") for value in fat)
            ).hexdigest(),
        },
        "root_entries": root_entries,
        "tree": nodes,
        "tree_truncated": truncated,
        "limits": {
            "sector_size": SECTOR,
            "fat16_clusters": [FAT16_MIN_CLUSTERS, FAT16_MAX_CLUSTERS],
            "content_hash_max_bytes": hash_limit,
            "max_depth": max_depth,
            "max_entries": max_entries,
        },
    }
    context = {"geometry": geometry, "fat": fat, "fat_bytes": fat_bytes,
               "root_entries": root_entries, "tree": nodes}
    return summary, context


def diff_sectors(pristine, guest):
    """Count differing 512-byte sectors and merge them into LBA ranges."""
    total = 0
    ranges = []
    ranges_truncated = False
    sizes_differ = pristine.stat().st_size != guest.stat().st_size
    with pristine.open("rb") as left, guest.open("rb") as right:
        offset = 0
        while True:
            first = left.read(DIFF_CHUNK_BYTES)
            second = right.read(DIFF_CHUNK_BYTES)
            if not first and not second:
                break
            common = min(len(first), len(second))
            for position in range(0, common, SECTOR):
                if first[position:position + SECTOR] == second[position:position + SECTOR]:
                    continue
                total += 1
                lba = offset // SECTOR + position // SECTOR
                if ranges and ranges[-1]["lba"] + ranges[-1]["sectors"] == lba:
                    ranges[-1]["sectors"] += 1
                elif len(ranges) < MAX_DIFF_RANGES:
                    ranges.append({"lba": lba, "sectors": 1})
                else:
                    ranges_truncated = True
            if len(first) != len(second):
                tail = max(len(first), len(second)) - common
                total += (tail + SECTOR - 1) // SECTOR
                offset += max(len(first), len(second))
                break
            offset += len(first)
    return {"sectors": total, "bytes": total * SECTOR,
            "ranges": ranges, "ranges_truncated": ranges_truncated,
            "sizes_differ": sizes_differ}


def diff_fat(before, after, geometry):
    allocated = []
    freed = []
    retargeted = []
    for cluster in range(2, geometry["cluster_count"] + 2):
        previous = before[cluster]
        current = after[cluster]
        if previous == 0 and current != 0:
            allocated.append(cluster)
        elif previous != 0 and current == 0:
            freed.append(cluster)
        elif previous != current:
            retargeted.append(cluster)
    per = geometry["sectors_per_cluster"]

    def sample(clusters, values=None):
        out = []
        for index, cluster in enumerate(clusters[:MAX_FAT_CHANGE_SAMPLES]):
            item = {"cluster": cluster,
                    "lba": geometry["data_lba"] + (cluster - 2) * per}
            if values is not None:
                item["before"] = f"0x{values[0][cluster]:04x}"
                item["after"] = f"0x{values[1][cluster]:04x}"
            out.append(item)
        return out

    return {
        "allocated_clusters": len(allocated),
        "freed_clusters": len(freed),
        "retargeted_clusters": len(retargeted),
        "allocated_bytes": len(allocated) * geometry["cluster_bytes"],
        "allocated_sample": sample(allocated),
        "freed_sample": sample(freed),
        "retargeted_sample": sample(retargeted, (before, after)),
        "samples_capped": MAX_FAT_CHANGE_SAMPLES,
    }


def _entry_key(entry):
    return entry["short_name"]


def diff_root_entries(before, after):
    previous = {_entry_key(entry): entry for entry in before}
    current = {_entry_key(entry): entry for entry in after}
    added = [current[key] for key in current.keys() - previous.keys()]
    removed = [previous[key] for key in previous.keys() - current.keys()]
    changed = []
    for key in previous.keys() & current.keys():
        fields = {}
        for field in ("name", "attributes", "first_cluster", "size"):
            if previous[key][field] != current[key][field]:
                fields[field] = {"before": previous[key][field],
                                 "after": current[key][field]}
        if fields:
            changed.append({"short_name": key, "fields": fields})
    order = lambda entry: entry["short_name"]
    return {"added": sorted(added, key=order),
            "removed": sorted(removed, key=order),
            "changed": sorted(changed, key=order)}


def diff_trees(before, after):
    previous = {node["path"]: node for node in before}
    current = {node["path"]: node for node in after}
    added = [current[path] for path in current.keys() - previous.keys()]
    removed = [previous[path] for path in previous.keys() - current.keys()]
    changed = []
    for path in previous.keys() & current.keys():
        fields = {}
        for field in ("type", "size", "first_cluster", "content_sha256",
                      "attribute_flags", "entries"):
            if previous[path].get(field) != current[path].get(field):
                fields[field] = {"before": previous[path].get(field),
                                 "after": current[path].get(field)}
        if fields:
            changed.append({"path": path, "fields": fields})
    return {"added": sorted(added, key=lambda node: node["path"]),
            "removed": sorted(removed, key=lambda node: node["path"]),
            "changed": sorted(changed, key=lambda node: node["path"])}


def diff_images(pristine_path, guest_path, pristine, pristine_context,
                guest, guest_context):
    """Compare two inspected cards; the guest must be a COPY of the pristine."""
    pristine_geometry = pristine_context["geometry"]
    guest_geometry = guest_context["geometry"]
    summaries = {
        "pristine_sha256": pristine["sha256"],
        "guest_sha256": guest["sha256"],
        "images_identical": pristine["sha256"] == guest["sha256"],
        "guest_wrote_to_sd": pristine["sha256"] != guest["sha256"],
        "changed_sectors": diff_sectors(pristine_path, guest_path),
        "mbr_changed": _first_sector(pristine_path)
                        != _first_sector(guest_path),
        "mbr_layout_before": pristine["mbr"]["layout"],
        "mbr_layout_after": guest["mbr"]["layout"],
        "partition_lba_before": pristine["mbr"]["partition_lba"],
        "partition_lba_after": guest["mbr"]["partition_lba"],
        "fat_copies_identical_before":
            pristine["fat16"]["fat_copies_identical"],
        "fat_copies_identical_after":
            guest["fat16"]["fat_copies_identical"],
        "fat_sha256_before": pristine["fat16"]["fat_sha256"],
        "fat_sha256_after": guest["fat16"]["fat_sha256"],
        "fat_table_changed":
            pristine["fat16"]["fat_sha256"] != guest["fat16"]["fat_sha256"],
    }
    boot_fields = {}
    for field in ("bytes_per_sector", "sectors_per_cluster", "reserved_sectors",
                  "num_fats", "root_entries", "total_sectors",
                  "sectors_per_fat", "hidden_sectors", "bpb_volume_label",
                  "volume_id", "fs_type"):
        if pristine_geometry[field] != guest_geometry[field]:
            boot_fields[field] = {"before": pristine_geometry[field],
                                  "after": guest_geometry[field]}
    summaries["boot_sector_field_changes"] = boot_fields
    summaries["boot_sector_changed"] = bool(boot_fields)
    summaries["root_entries"] = diff_root_entries(
        pristine_context["root_entries"], guest_context["root_entries"])
    summaries["tree"] = diff_trees(pristine_context["tree"],
                                   guest_context["tree"])
    summaries["fat"] = diff_fat(pristine_context["fat"], guest_context["fat"],
                                guest_geometry)
    summaries["free_space"] = {
        "before": {"free_clusters": pristine["fat16"]["free_clusters"],
                   "free_bytes": pristine["fat16"]["free_bytes"]},
        "after": {"free_clusters": guest["fat16"]["free_clusters"],
                  "free_bytes": guest["fat16"]["free_bytes"]},
        "free_bytes_delta": (guest["fat16"]["free_bytes"]
                             - pristine["fat16"]["free_bytes"]),
    }
    summaries["guest_created_entries"] = len(summaries["tree"]["added"])
    summaries["guest_removed_entries"] = len(summaries["tree"]["removed"])
    summaries["guest_changed_entries"] = len(summaries["tree"]["changed"])
    return summaries


def analyze(pristine_path, guest_path=None, hash_limit=DEFAULT_HASH_LIMIT,
            max_depth=DEFAULT_MAX_DEPTH, max_entries=DEFAULT_MAX_ENTRIES):
    """Inspect the pristine card and, when given, the guest's post-run copy."""
    pristine_path = Path(pristine_path)
    pristine, pristine_context = inspect_image(
        pristine_path, hash_limit, max_depth, max_entries)
    report = {
        "pristine": pristine,
        "guest": None,
        "diff": None,
        "proves": ["on-card FAT16 layout and free space",
                   "which sectors, FAT entries, directories and file bytes "
                   "the guest changed"],
        "does_not_prove": ["that the guest read any file",
                           "that the guest mounted the volume",
                           "anything about the UI, panel or boot"],
    }
    guest_context = None
    if guest_path is not None:
        guest_path = Path(guest_path)
        if guest_path.resolve() == pristine_path.resolve():
            raise ValueError("guest card must be a separate copy, not the "
                             "pristine image")
        guest, guest_context = inspect_image(
            guest_path, hash_limit, max_depth, max_entries)
        report["guest"] = guest
        report["diff"] = diff_images(pristine_path, guest_path, pristine,
                                     pristine_context, guest, guest_context)
    return report, pristine_context, guest_context


def _safe_relative(path):
    parts = [part for part in path.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        raise ValueError(f"unsafe extraction path {path!r}")
    return Path(*parts)


def extract_changed(guest_path, guest_context, diff, out_dir, max_bytes):
    """Write added/changed guest files below out_dir; never overwrite."""
    out_dir = Path(out_dir)
    geometry = guest_context["geometry"]
    fat = guest_context["fat"]
    wanted = [node for node in diff["tree"]["added"] + diff["tree"]["changed"]
              if node["type"] == "file"]
    extracted = []
    with Path(guest_path).open("rb") as handle:
        for node in wanted:
            relative = _safe_relative(node["path"])
            target = out_dir / relative
            if node["size"] > max_bytes:
                extracted.append({"path": node["path"], "extracted": False,
                                  "reason": f"larger than {max_bytes} bytes"})
                continue
            if target.exists():
                raise FileExistsError(
                    f"refuse overwriting extracted guest file: {target}")
            content, _ = read_file(handle, geometry, fat, node["first_cluster"],
                                   node["size"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            extracted.append({"path": node["path"], "extracted": True,
                              "output": str(target.resolve()),
                              "bytes": len(content),
                              "sha256": hashlib.sha256(content).hexdigest()})
    return extracted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pristine", type=Path, required=True,
                        help="pristine SD image (read-only)")
    parser.add_argument("--guest-card", type=Path,
                        help="the guest's SD copy after a run (read-only)")
    parser.add_argument("--report", type=Path,
                        help="write the JSON report to this path as well")
    parser.add_argument("--extract-dir", type=Path,
                        help="extract added/changed guest files below this dir")
    parser.add_argument("--extract-max-bytes", type=int,
                        default=DEFAULT_HASH_LIMIT)
    parser.add_argument("--hash-limit", type=int, default=DEFAULT_HASH_LIMIT)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    parser.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ENTRIES)
    args = parser.parse_args()
    report, _, guest_context = analyze(
        args.pristine, args.guest_card, args.hash_limit, args.max_depth,
        args.max_entries)
    if args.extract_dir is not None:
        if guest_context is None:
            parser.error("--extract-dir requires --guest-card")
        report["extracted"] = extract_changed(
            args.guest_card, guest_context, report["diff"], args.extract_dir,
            args.extract_max_bytes)
    text = json.dumps(report, indent=2) + "\n"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text)
    print(text, flush=True)


if __name__ == "__main__":
    main()
