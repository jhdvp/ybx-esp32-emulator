"""Lossless 1bit SSD1677 frame rotation and PNG export, no Pillow requirement."""
import struct
import unittest
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from epd_frame_io import ssd1677_landscape_to_portrait, save_mono_png

class EPDFrameIoTests(unittest.TestCase):
    def test_transpose_preserves_polarity_and_pixel_coordinates(self):
        # 16x8 pixels, 8 rows, black at original x=13,y=6
        src=bytearray(b"\xff"*16)
        src[6*2+1] &= ~(0x80>>(13%8))
        dst=ssd1677_landscape_to_portrait(bytes(src),16,8)
        self.assertEqual(len(dst),16)
        # Portrait width 8, height16; x'=6, y'=13 (transposed).
        self.assertEqual(dst[13] & (0x80>>6),0)
        self.assertEqual(sum(byte.bit_count() for byte in dst),127)
    def test_reject_invalid_frames(self):
        with self.assertRaisesRegex(ValueError,"dimensions"):
            ssd1677_landscape_to_portrait(b"bad",800,480)
    def test_png_scanline_round_trip(self):
        data=bytes((0x00,0xff))*8 # 16x8 1bit
        with TemporaryDirectory() as temp:
            p=Path(temp)/"capture.png"
            save_mono_png(p,data,16,8)
            raw=p.read_bytes()
        self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack_from(">II",raw,16),(16,8))
        # PNG IDAT starts after the 25-byte IHDR chunk.
        size=struct.unpack_from(">I",raw,33)[0]
        payload=zlib.decompress(raw[41:41+size])
        self.assertEqual(payload,b"".join(
            b"\x00"+data[y*2:(y+1)*2] for y in range(8)))
