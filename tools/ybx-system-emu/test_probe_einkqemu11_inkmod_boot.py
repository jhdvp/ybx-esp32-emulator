import importlib.util
import tempfile
import unittest
from pathlib import Path

PATH = Path(__file__).with_name("probe_einkqemu11_inkmod_boot.py")
SPEC = importlib.util.spec_from_file_location("eink_inkmod", PATH)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


class BootProbeTests(unittest.TestCase):
    def test_parse_qemu_gpio_input(self):
        self.assertEqual(MOD.parse_gpio_input("000000006000403c: ffdfffff\n"),
                         int("ffdfffff", 16))
        self.assertEqual(MOD.parse_gpio_input("0x6000403c: 0x00200000"),
                         0x00200000)

    def test_gpio_parser_refuses_unknown_address(self):
        with self.assertRaises(ValueError):
            MOD.parse_gpio_input("0x60004038: 0xffffffff")

    def test_ppm_genuine_nonuniform_pixels(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "frame.ppm"
            path.write_bytes(b"P6\n2 1\n255\n" + bytes([0, 0, 0, 255, 255, 255]))
            got = MOD.ppm_pixels(path)
            self.assertEqual((got["width"], got["height"]), (2, 1))
            self.assertTrue(got["nonuniform"])

    def test_ppm_uniform_is_not_a_rendered_screen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "blank.ppm"
            path.write_bytes(b"P6\n2 1\n255\n" + bytes([255] * 6))
            self.assertFalse(MOD.ppm_pixels(path)["nonuniform"])

    def test_ppm_rejects_truncated_image(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.ppm"
            path.write_bytes(b"P6\n2 1\n255\n" + bytes([255] * 5))
            with self.assertRaises(ValueError):
                MOD.ppm_pixels(path)


if __name__ == "__main__":
    unittest.main()
