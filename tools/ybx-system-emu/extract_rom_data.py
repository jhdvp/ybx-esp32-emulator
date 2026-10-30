#!/usr/bin/env python3
"""Extract ESP32-S3 revision 0 ROM writable initializers from a local ELF.

This is a QEMU-only test artifact; never commit the generated ROM image.
The ROM ELF's first PT_LOAD overlaps the ELF header, so copying PT_LOAD
contents does not initialize its writable interface sections correctly.
"""
from pathlib import Path
import argparse
import hashlib
import struct

BASE = 0x3FCD7000
SIZE = 0x19000
LEGACY_POINTER = 0x3FCEFFE4


def extract_range(path: Path, base: int, size: int, legacy_pointer: int) -> bytes:
    """Reconstruct ROM-owned writable DRAM initializers for a chip revision."""
    content = path.read_bytes()
    if len(content) < 52 or content[:6] != b"\x7fELF\x01\x01":
        raise ValueError("expected little-endian ELF32 ROM")
    shoff = struct.unpack_from("<I", content, 32)[0]
    shentsize, shnum = struct.unpack_from("<HH", content, 46)
    if (shentsize != 40 or shnum < 1 or shnum > 256 or
            shoff + shentsize * shnum > len(content)):
        raise ValueError("invalid ELF32 section-header table")
    result = bytearray(size)
    occupied = bytearray(size)
    count = 0
    for index in range(shnum):
        _name, kind, flags, address, offset, length, *_ = struct.unpack_from(
            "<IIIIIIIIII", content, shoff + index * shentsize)
        # Some Espressif ROM interface sections are SHF_WRITE but NOT
        # SHF_ALLOC. Both are required for a complete ROM memory image.
        if kind != 1 or not (flags & 0x1) or length == 0:
            continue
        if address < base or address >= base + size:
            continue
        if (length > base + size - address or
                offset > len(content) or length > len(content) - offset):
            raise ValueError("ROM writable initializer outside image")
        start = address - base
        segment = content[offset:offset + length]
        for j, value in enumerate(segment):
            if occupied[start + j] and result[start + j] != value:
                raise ValueError("overlapping inconsistent ROM initializers")
            result[start + j] = value
            occupied[start + j] = 1
        count += 1
    pointer_offset = legacy_pointer - base
    value = struct.unpack_from("<I", result, pointer_offset)[0]
    if count < 2 or not (base <= value < base + size):
        raise ValueError("ROM spiflash legacy initializer missing or invalid")
    return bytes(result)


def extract(path: Path) -> bytes:
    """ESP32-S3 legacy interface retained for existing regression tests."""
    return extract_range(path, BASE, SIZE, LEGACY_POINTER)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = extract(args.elf)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(result)
    print(f"ROM init: {len(result)} bytes; SHA256={hashlib.sha256(result).hexdigest()}")
    print(f"Local-only: {args.out}")


if __name__ == "__main__":
    main()
