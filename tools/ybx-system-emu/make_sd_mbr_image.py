#!/usr/bin/env python3
"""Wrap a disposable FAT16 superfloppy image as MBR partition #1.

FreeInk X4 Pro calls SdFat FsVolume.begin(dev) with default part=1;
part=0 would consume a FAT boot sector at LBA0, but is not used.
This tool leaves the original image unchanged and never touches real media.
"""
import argparse
import hashlib
import json
import shutil
import struct
from pathlib import Path

SECTOR=512
PART_START=2048
PART_TYPE=0x06   # FAT16 with normal CHS-compatible partition type

def make_image(source:Path,dest:Path)->dict:
    source=source.resolve(strict=True)
    dest=dest.resolve()
    if source==dest:
        raise ValueError("source and destination must be different")
    if dest.exists():
        raise FileExistsError(f"refuse to overwrite image: {dest}")
    size=source.stat().st_size
    if size<=SECTOR or size%SECTOR:
        raise ValueError("source FAT volume must have whole 512-byte sectors")
    with source.open("rb") as inp:
        boot=bytearray(inp.read(SECTOR))
        if boot[510:512]!=b"\x55\xaa":
            raise ValueError("FAT boot signature 55aa absent")
        if struct.unpack_from("<H",boot,11)[0]!=SECTOR:
            raise ValueError("FAT volume sector size must be 512")
        if boot[54:59]!=b"FAT16":
            raise ValueError("this builder only wraps FAT16 volumes")
        total=struct.unpack_from("<H",boot,19)[0] or struct.unpack_from("<I",boot,32)[0]
        if total!=size//SECTOR:
            raise ValueError(f"FAT BPB sector count {total} differs from image {size//SECTOR}")
        if PART_START+total>0xffffffff:
            raise ValueError("partition exceeds MBR LBA range")
        struct.pack_into("<I",boot,28,PART_START)
        mbr=bytearray(SECTOR)
        # Partition entry 1 at 0x1be: no active boot, plausible CHS geometry.
        struct.pack_into("<B3sB3sII",mbr,446,0,b"\x00\x21\x00",
                         PART_TYPE,b"\xfe\xff\xff",PART_START,total)
        mbr[510:512]=b"\x55\xaa"
        dest.parent.mkdir(parents=True,exist_ok=True)
        with dest.open("xb") as out:
            out.write(mbr)
            out.write(bytes(PART_START*SECTOR-SECTOR))
            out.write(boot)
            shutil.copyfileobj(inp,out,1024*1024)
            # QEMU's SD card model requires a power-of-two capacity.
            # Keep the FAT16 partition size unchanged; pad UNALLOCATED
            # space after it to the next power-of-two card capacity.
            full_size=out.tell()
            out.truncate(1 << (full_size-1).bit_length())
    with dest.open("rb") as result:
        result.seek(446)
        entry=result.read(16)
        result.seek(PART_START*SECTOR)
        sector0=result.read(512)
    assert entry[4]==PART_TYPE and entry[8:12]==struct.pack("<I",PART_START)
    assert sector0[54:59]==b"FAT16" and sector0[510:512]==b"\x55\xaa"
    with source.open("rb") as f:
        source_digest=hashlib.file_digest(f,"sha256").hexdigest()
    with dest.open("rb") as f:
        output_digest=hashlib.file_digest(f,"sha256").hexdigest()
    return {
        "source":str(source),"output":str(dest),
        "source_sha256":source_digest,
        "output_sha256":output_digest,
        "partition_index":1,"partition_type":hex(PART_TYPE),
        "partition_lba":PART_START,"partition_sectors":total,
        "volume_bytes":size,"total_disk_bytes":dest.stat().st_size,
        "bpb_hidden_sectors":struct.unpack_from("<I",sector0,28)[0],
        "mbr_boot_signature":mbr[510:512].hex(),
        "fat_boot_signature":sector0[510:512].hex(),
        "source_untouched":True,
        "guest_mount_verified":False,
    }

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source",type=Path,required=True)
    p.add_argument("--out",type=Path,required=True)
    p.add_argument("--report",type=Path,required=True)
    a=p.parse_args()
    record=make_image(a.source,a.out)
    a.report.parent.mkdir(parents=True,exist_ok=True)
    a.report.write_text(json.dumps(record,indent=2)+"\n")
    print(json.dumps(record,indent=2))

if __name__=="__main__":
    main()
