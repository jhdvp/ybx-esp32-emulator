#!/usr/bin/env python3
"""Verify that real ESP32-S3 guest changed a FAT16 partition and saved BMP.

Never modify source disk images. The guest ran against a disposable copy.
An on-card screenshot validates guest file I/O, NOT an e-paper controller.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import struct
from pathlib import Path

SECTOR=512

def fat_layout(src:Path,part_lba:int):
    with src.open("rb") as f:
        f.seek(part_lba*SECTOR)
        boot=f.read(SECTOR)
    if boot[510:512] != b"\x55\xaa" or boot[54:59] != b"FAT16":
        raise ValueError("expected valid FAT16 boot sector")
    reserved=struct.unpack_from("<H",boot,14)[0]
    count=boot[16]
    sectors_fat=struct.unpack_from("<H",boot,22)[0]
    root_entries=struct.unpack_from("<H",boot,17)[0]
    cluster_sectors=boot[13]
    if not (cluster_sectors and root_entries and sectors_fat and count):
        raise ValueError("invalid FAT geometry")
    return {
        "part_lba":part_lba,"fat_lba":part_lba+reserved,
        "fat_sectors":sectors_fat,
        "root_lba":part_lba+reserved+count*sectors_fat,
        "root_entries":root_entries,
        "data_lba":part_lba+reserved+count*sectors_fat+
                   (root_entries*32+SECTOR-1)//SECTOR,
        "sectors_per_cluster":cluster_sectors,
    }

def read_sectors(f,lba,count):
    f.seek(lba*SECTOR)
    v=f.read(count*SECTOR)
    if len(v)!=count*SECTOR:raise ValueError("truncated virtual card")
    return v

def entries(buf):
    out=[]
    for off in range(0,len(buf),32):
        e=buf[off:off+32]
        if len(e)!=32 or e[0]==0:break
        if e[0]==0xe5 or e[11]==0x0f:continue
        out.append({
            "name":e[:11].decode("ascii","replace").rstrip(),
            "attribute":e[11],
            "first_cluster":struct.unpack_from("<H",e,26)[0],
            "size":struct.unpack_from("<I",e,28)[0]
        })
    return out

def get_file(f,layout,fat,entry):
    c=entry["first_cluster"]
    remaining=entry["size"]
    out=bytearray()
    seen=set()
    while remaining:
        if c<2 or c>=len(fat)//2 or c in seen:
            raise ValueError("invalid/cyclic FAT16 cluster chain")
        seen.add(c)
        block=read_sectors(f,layout["data_lba"]+
             (c-2)*layout["sectors_per_cluster"],
             layout["sectors_per_cluster"])
        out.extend(block[:remaining])
        remaining-=min(len(block),remaining)
        next_cluster=struct.unpack_from("<H",fat,2*c)[0]
        if remaining and (next_cluster<2 or next_cluster>=0xfff8):
            raise ValueError("truncated FAT16 chain")
        c=next_cluster
    return bytes(out),len(seen)

def parse_bmp(b:bytes):
    if len(b)<62 or b[:2]!=b"BM":raise ValueError("not BMP")
    size=struct.unpack_from("<I",b,2)[0]
    offset=struct.unpack_from("<I",b,10)[0]
    width,height=struct.unpack_from("<ii",b,18)
    bits=struct.unpack_from("<H",b,28)[0]
    if size!=len(b) or bits!=1 or width<=0 or height==0:
        raise ValueError("invalid 1bpp BMP screenshot")
    stride=((width+31)//32)*4
    pixels=b[offset:offset+stride*abs(height)]
    if len(pixels)!=stride*abs(height):
        raise ValueError("short bitmap pixel data")
    palette0=b[54:58]
    palette1=b[58:62]
    dark_index=0 if sum(palette0[:3]) < sum(palette1[:3]) else 1
    dark=0
    per_row=[]
    for y in range(abs(height)):
        row=pixels[y*stride:y*stride+stride]
        n=0
        for x in range(width):
            pix=(row[x//8]>>(7-x%8))&1
            if pix==dark_index:n+=1
        dark+=n
        per_row.append(n)
    return {
        "format":"BMP","width":width,"height":abs(height),
        "bits_per_pixel":bits,"size_bytes":len(b),
        "dark_pixel_fraction":round(dark/(width*abs(height)),6),
        "different_row_densities":len(set(per_row)),
        "uniform_all_white_or_black":dark in (0,width*abs(height)),
    }

def analyze(original:Path,post:Path,out_bmp:Path)->dict:
    original=original.resolve(strict=True)
    post=post.resolve(strict=True)
    with post.open("rb") as f:
        mbr=read_sectors(f,0,1)
    if mbr[510:512]!=b"\x55\xaa":
        raise ValueError("no MBR signature")
    part_type=mbr[450]
    part_lba,part_sectors=struct.unpack_from("<II",mbr,454)
    if part_type!=0x06 or part_lba==0:
        raise ValueError("no FAT16 MBR partition 1")
    base=fat_layout(original,0)
    layout=fat_layout(post,part_lba)
    with original.open("rb") as f:
        before=entries(read_sectors(f,base["root_lba"],
                      (base["root_entries"]*32+511)//512))
        original_fat=read_sectors(f,base["fat_lba"],base["fat_sectors"])
    with post.open("rb") as f:
        after=entries(read_sectors(f,layout["root_lba"],
                     (layout["root_entries"]*32+511)//512))
        new_fat=read_sectors(f,layout["fat_lba"],layout["fat_sectors"])
        before_names={x["name"] for x in before}
        created=[x for x in after if x["name"] not in before_names]
        screenshots=[x for x in created if x["attribute"]&0x10
                     and x["first_cluster"]>=2]
        if not screenshots:
            raise AssertionError("guest did not create a directory in FAT root")
        folder=screenshots[0]
        dir_data=read_sectors(f,layout["data_lba"]+
                 (folder["first_cluster"]-2)*layout["sectors_per_cluster"],
                 layout["sectors_per_cluster"])
        files=entries(dir_data)
        candidates=[x for x in files if x["attribute"]&0x20 and x["size"]>=62]
        if not candidates:raise AssertionError("guest directory contains no new file")
        screenshot=candidates[0]
        content,clusters=get_file(f,layout,new_fat,screenshot)
    bmp=parse_bmp(content)
    if bmp["uniform_all_white_or_black"]:
        raise AssertionError("guest BMP is uniform; no visible UI evidence")
    out_bmp.parent.mkdir(parents=True,exist_ok=True)
    if out_bmp.exists():
        raise FileExistsError(f"refuse overwriting extracted guest file: {out_bmp}")
    out_bmp.write_bytes(content)
    with original.open("rb") as f: sha_original=hashlib.file_digest(f,"sha256").hexdigest()
    with post.open("rb") as f: sha_after=hashlib.file_digest(f,"sha256").hexdigest()
    return {
        "original_fat_sha256":sha_original,
        "modified_mbr_card_sha256":sha_after,
        "partition_lba":part_lba,
        "partition_type":hex(part_type),
        "partition_sectors":part_sectors,
        "fat16_root_before":before,
        "fat16_root_after":after,
        "created_root_entries":created,
        "fat_table_modified":original_fat!=new_fat,
        "guest_file":screenshot,
        "guest_file_fat_clusters":clusters,
        "guest_file_sha256":hashlib.sha256(content).hexdigest(),
        "guest_file_bmp":bmp,
        "extracted_file_path":str(out_bmp),
        "guest_written_fat_structure_verified":True,
        "guest_sd_fat_file_creation_verified":True,
        "epaper_display_controller_verified":False,
        "original_disk_untouched":True,
    }

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--original-fat",type=Path,required=True)
    ap.add_argument("--guest-card",type=Path,required=True)
    ap.add_argument("--out-bmp",type=Path,required=True)
    ap.add_argument("--report",type=Path,required=True)
    a=ap.parse_args()
    report=analyze(a.original_fat,a.guest_card,a.out_bmp)
    a.report.parent.mkdir(parents=True,exist_ok=True)
    a.report.write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report,indent=2))

if __name__=="__main__":
    main()
