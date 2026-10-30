"""Host-only regression tests for MBR image builder and guest BMP parser."""
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from make_sd_mbr_image import make_image, PART_START
from analyze_guest_fat_image import parse_bmp, entries, fat_layout


def bmp_fixture():
    width=8
    height=8
    bits=32
    data=bytearray(62+bits)
    data[:2]=b"BM"
    struct.pack_into("<I",data,2,len(data))
    struct.pack_into("<I",data,10,62)
    struct.pack_into("<I",data,14,40)
    struct.pack_into("<ii",data,18,width,height)
    struct.pack_into("<H",data,26,1)
    struct.pack_into("<H",data,28,1)
    data[58:62]=b"\xff\xff\xff\x00"
    for row in range(height):
        data[62+row*4]=0x55 if row%2==0 else 0xaa
    return bytes(data)


class MBRSdFatTests(unittest.TestCase):
    def source(self,folder):
        p=Path(folder)/"original.img"
        b=bytearray(512*16)
        b[0:3]=b"\xeb\x3c\x90"
        b[54:62]=b"FAT16   "
        b[510:512]=b"\x55\xaa"
        struct.pack_into("<H",b,11,512)
        b[13]=1
        struct.pack_into("<H",b,14,1)
        b[16]=1
        struct.pack_into("<H",b,17,16)
        struct.pack_into("<H",b,19,16)
        struct.pack_into("<H",b,22,1)
        p.write_bytes(b)
        return p

    def test_mbr_wrapping_with_qemu_power_two_capacity(self):
        with TemporaryDirectory() as temp:
            src=self.source(temp)
            dst=Path(temp)/"sd-mbr.img"
            r=make_image(src,dst)
            self.assertEqual(r["partition_lba"],PART_START)
            self.assertEqual(r["partition_type"],"0x6")
            self.assertEqual(dst.stat().st_size & (dst.stat().st_size-1),0)
            self.assertTrue(r["source_untouched"])
            with dst.open("rb") as f:
                mbr=f.read(512)
                f.seek(PART_START*512)
                boot=f.read(512)
            self.assertEqual(mbr[510:512],b"\x55\xaa")
            self.assertEqual(struct.unpack_from("<I",mbr,454)[0],PART_START)
            self.assertEqual(struct.unpack_from("<I",boot,28)[0],PART_START)
            self.assertEqual(fat_layout(dst,PART_START)["root_lba"],PART_START+2)
            self.assertEqual(src.stat().st_size,512*16)

    def test_never_overwrite_existing_or_invalid_source(self):
        with TemporaryDirectory() as temp:
            src=self.source(temp)
            with self.assertRaises(ValueError):
                make_image(src,src)
            dst=Path(temp)/"sd-mbr.img"
            dst.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                make_image(src,dst)
            self.assertEqual(dst.read_bytes(),b"keep")
            dst.unlink()
            src.write_bytes(bytes(512*16))
            with self.assertRaisesRegex(ValueError,"signature"):
                make_image(src,dst)

    def test_bmp_unambiguous_nonblank_and_truncation(self):
        image=parse_bmp(bmp_fixture())
        self.assertEqual((image["width"],image["height"],image["bits_per_pixel"]),
                         (8,8,1))
        self.assertFalse(image["uniform_all_white_or_black"])
        self.assertGreater(image["different_row_densities"],0)
        with self.assertRaisesRegex(ValueError,"invalid 1bpp BMP"):
            parse_bmp(bmp_fixture()[:-1])

    def test_short_fat_directory_entries_skip_lfn(self):
        lfn=bytearray(32)
        lfn[0]=0x41
        lfn[11]=0x0f
        file=bytearray(32)
        file[:11]=b"SCREEN~1BMP"
        file[11]=0x20
        struct.pack_into("<H",file,26,3)
        struct.pack_into("<I",file,28,48062)
        e=entries(bytes(lfn+file)+bytes(32))
        self.assertEqual(len(e),1)
        self.assertEqual(e[0]["size"],48062)
