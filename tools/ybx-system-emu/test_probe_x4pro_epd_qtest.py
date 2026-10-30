"""Validate generated 1-bit BMP stays a meaningful EPD RAM export."""
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from probe_x4pro_epd_qtest import save_bmp,W,H,SIZE

class EPDScreenshotTests(unittest.TestCase):
    def test_bmp_matches_ssd1677_800x480_frame_and_reverses_rows(self):
        frame=bytearray(bytes((0xff,))*SIZE)
        frame[:8]=bytes((0,0x11,0x22,0x33,0x44,0x55,0x66,0x77))
        with TemporaryDirectory() as temp:
            p=Path(temp)/"epd.bmp"
            save_bmp(p,bytes(frame))
            d=p.read_bytes()
        self.assertEqual(d[:2],b"BM")
        self.assertEqual(struct.unpack_from("<I",d,2)[0],62+SIZE)
        self.assertEqual(struct.unpack_from("<ii",d,18),(W,H))
        self.assertEqual(struct.unpack_from("<H",d,28)[0],1)
        self.assertEqual(len(d),62+SIZE)
        self.assertEqual(d[62:62+W//8],b"\xff"*(W//8))
        self.assertEqual(d[-W//8:-W//8+8],bytes(frame[:8]))

    def test_rejects_non_frame(self):
        with TemporaryDirectory() as temp:
            with self.assertRaises(AssertionError):
                save_bmp(Path(temp)/"bad.bmp",b"short")
