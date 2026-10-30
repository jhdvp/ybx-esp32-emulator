"""Lossless readout of virtual SSD1677 RAM as PNG, no display emulation.

Convert board-native 800x480 raw RAM bit orientation into YBX logical
480x800 portrait by transposing X/Y (clockwise rotate + horizontal mirror).
The transformation reorders genuine guest pixels; it never synthesizes UI.
"""
from pathlib import Path
import struct
import zlib


def ssd1677_landscape_to_portrait(frame:bytes, width:int=800, height:int=480)->bytes:
    if width<=0 or height<=0 or width%8 or height%8:
        raise ValueError("1bpp packed dimensions must be positive and divisible by 8")
    if len(frame)!=width*height//8:
        raise ValueError("SSD1677 frame is not the expected packed dimensions")
    src_row=width//8
    dst_row=height//8
    # Destination X = source Y; destination Y = source X.
    # Preserves MSB-first pixels and original black/white polarity.
    dst=bytearray(b"\xff"*(width*height//8))
    for y in range(height):
        src_offset=y*src_row
        d_byte=y//8
        d_mask=0x80>>(y%8)
        for x in range(width):
            if not (frame[src_offset+x//8] & (0x80>>(x%8))):
                dst[x*dst_row+d_byte] &= ~d_mask
    return bytes(dst)


def save_mono_png(path:Path,frame:bytes,width:int,height:int)->None:
    if width<=0 or height<=0 or width%8 or len(frame)!=width*height//8:
        raise ValueError("invalid packed 1bpp PNG dimensions")
    def chunk(name:bytes,data:bytes)->bytes:
        return struct.pack(">I",len(data))+name+data+struct.pack(
            ">I",zlib.crc32(name+data)&0xffffffff)
    scanline=width//8
    payload=b"".join(
        b"\x00"+frame[y*scanline:(y+1)*scanline]
        for y in range(height))
    png=(b"\x89PNG\r\n\x1a\n"
         +chunk(b"IHDR",struct.pack(">IIBBBBB",width,height,1,0,0,0,0))
         +chunk(b"IDAT",zlib.compress(payload,9))
         +chunk(b"IEND",b""))
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(png)
