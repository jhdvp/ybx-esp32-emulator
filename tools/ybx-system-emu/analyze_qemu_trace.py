#!/usr/bin/env python3
"""Correlate QEMU Xtensa instruction traces with a REAL ESP32-S3 factory app.

Translation-block logs prove machine-code reachability, not app readiness.
A flash IROM address only counts if the firmware image declares that segment.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct

APP_OFFSET = 0x10000
ESP_IMAGE_HEADER_SIZE = 24
SEGMENT_HEADER_SIZE = 8
APP_IROM_START = 0x42000000
APP_IROM_END = 0x43000000
INSTRUCTION = re.compile(r"^0x([0-9a-fA-F]{8}):")


def app_irom_ranges(factory: Path) -> list[tuple[int, int]]:
    data = factory.read_bytes()
    if len(data) < APP_OFFSET + ESP_IMAGE_HEADER_SIZE or data[APP_OFFSET] != 0xe9:
        raise ValueError("not a merged ESP32 factory image")
    segments = data[APP_OFFSET + 1]
    if not 1 <= segments <= 32:
        raise ValueError("unexpected ESP image segment count")
    offset = APP_OFFSET + ESP_IMAGE_HEADER_SIZE
    ranges = []
    for _ in range(segments):
        if offset + SEGMENT_HEADER_SIZE > len(data):
            raise ValueError("truncated ESP32 segment header")
        addr, size = struct.unpack_from("<II", data, offset)
        offset += SEGMENT_HEADER_SIZE
        if size > len(data) - offset:
            raise ValueError("truncated ESP32 segment payload")
        if APP_IROM_START <= addr < APP_IROM_END:
            if size == 0 or size > APP_IROM_END - addr:
                raise ValueError("invalid mapped IROM range")
            ranges.append((addr, addr + size))
        offset += size
    if not ranges:
        raise ValueError("factory image has no mapped application IROM segment")
    return ranges


def analyze(trace: Path, ranges: list[tuple[int, int]]) -> dict:
    total = 0
    matched = 0
    first = None
    low = high = None
    rom_fatal = False
    bootloader_iram = False
    with trace.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            match = INSTRUCTION.match(line)
            if not match:
                continue
            pc = int(match.group(1), 16)
            total += 1
            if pc == 0x40050f3f:
                rom_fatal = True
            if 0x403c0000 <= pc < 0x403d0000:
                bootloader_iram = True
            if any(start <= pc < end for start, end in ranges):
                matched += 1
                if first is None:
                    first = pc
                low = pc if low is None else min(low, pc)
                high = pc if high is None else max(high, pc)
    return {
        "guest_instruction_records": total,
        "app_irom_instruction_records": matched,
        "app_irom_reached": matched > 0,
        "app_irom_first_pc": f"0x{first:08x}" if first is not None else None,
        "app_irom_min_pc": f"0x{low:08x}" if low is not None else None,
        "app_irom_max_pc": f"0x{high:08x}" if high is not None else None,
        "prior_rom_fatal_loop_seen": rom_fatal,
        "rom_to_iram_seen": bootloader_iram,
        "application_ready_verified": False,
        "peripherals_verified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factory-image", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.trace, app_irom_ranges(args.factory_image))
    with args.factory_image.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    result.update({
        "flash_source_sha256": digest,
        "application_irom_segments": [
            {"start": f"0x{start:08x}", "end_exclusive": f"0x{end:08x}"}
            for start, end in app_irom_ranges(args.factory_image)
        ],
        "trace": str(args.trace),
    })
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
