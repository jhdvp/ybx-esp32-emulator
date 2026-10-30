#!/usr/bin/env python3
"""Extract the local Espressif ROM ELF's ROM-address PT_LOAD segments for QEMU.

This does NOT extract ROM data from a real device. Use the locally installed
toolchain ROM ELF for the matching silicon revision. Never commit generated ROM.
"""
import argparse
from pathlib import Path
import struct
import hashlib

ROM_BASE=0x40000000
ROM_SIZE=0x60000

def extract(path:Path)->bytes:
    data=path.read_bytes()
    if len(data)<52 or data[:6] != b"\x7fELF\x01\x01":
        raise ValueError("expected little-endian 32-bit ROM ELF")
    phoff=struct.unpack_from("<I",data,28)[0]
    phentsize,phnum=struct.unpack_from("<HH",data,42)
    if phentsize!=32 or phnum>256 or phoff+phentsize*phnum>len(data):
        raise ValueError("invalid ELF32 program-header table")
    raw=bytearray(b"\xff"*ROM_SIZE)
    loaded=0
    for n in range(phnum):
        kind,offset,vaddr,paddr,size,memsize,flags,alignment=struct.unpack_from("<IIIIIIII",data,phoff+n*phentsize)
        if kind!=1 or not size or not (ROM_BASE<=paddr and paddr+size<=ROM_BASE+ROM_SIZE):
            continue
        if offset+size>len(data):
            raise ValueError("truncated loadable program segment")
        start=paddr-ROM_BASE
        raw[start:start+size]=data[offset:offset+size]
        loaded+=size
    if loaded<1024:
        raise ValueError("ROM ELF contains no sufficient loadable ROM code")
    return bytes(raw)

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--elf",type=Path,required=True)
    ap.add_argument("--out",type=Path,required=True)
    args=ap.parse_args()
    content=extract(args.elf)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_bytes(content)
    print(f"ROM bytes: {len(content)} SHA256: {hashlib.sha256(content).hexdigest()}")
    print(f"Local-only test image: {args.out}")

if __name__=="__main__":
    main()
