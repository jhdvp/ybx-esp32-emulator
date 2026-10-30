import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("system_emu", Path(__file__).with_name("run.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

class ImageTests(unittest.TestCase):
    def make_img(self, directory, mode="factory"):
        path = Path(directory) / "test.bin"
        buf = bytearray(b"\xff" * 0x10080)
        buf[0] = 0xE9
        buf[0x8000:0x8002] = b"\xaa\x50"
        buf[0x10000] = 0xE9 if mode=="factory" else 0xFF
        path.write_bytes(buf)
        return path

    def test_factory_image_requires_partition_and_app(self):
        with tempfile.TemporaryDirectory() as temp:
            p = self.make_img(temp)
            self.assertEqual(len(runner.validate_image(p)), 64)
            data = bytearray(p.read_bytes())
            data[0x8000] = 0
            p.write_bytes(data)
            with self.assertRaisesRegex(ValueError, "partition"):
                runner.validate_image(p)

    def test_rejects_app_only_image(self):
        with tempfile.TemporaryDirectory() as temp:
            p = self.make_img(temp,"app_only")
            with self.assertRaisesRegex(ValueError, "application image"):
                runner.validate_image(p)

class LogTests(unittest.TestCase):
    def test_real_boot_not_peripheral_pass(self):
        log = """ESP-ROM:esp32c3-api1-20210207
rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)
entry 0x403cbf10
[   663][E][Preferences.cpp:47] begin(): nvs_open failed: NOT_FOUND
[INFO esp_emu] Timeout reached after 9s
"""
        r = runner.classify_log(log, "c3", 0, False)
        self.assertEqual(r["rom"], "observed")
        self.assertEqual(r["application_code"], "observed")
        self.assertEqual(r["sd_card_io"], "not_verified")
        self.assertEqual(r["epaper"], "not_emulated")
        self.assertEqual(r["tls_handshake"], "not_verified")
        self.assertTrue(r["markers"]["emulator_timeout"])

    def test_sdmmc_error_not_silent_success(self):
        log = "sdmmc_periph: sdmmc_host_init: sdmmc_host_reset returned 0x107"
        r = runner.classify_log(log, "s3", 0, True)
        self.assertEqual(r["sdmmc"], "driver_error")
        self.assertEqual(r["rom"], "not_observed")

    def test_register_stub_is_not_a_touch_model(self):
        r = runner.classify_log("entry 0x403cbf10", "s3", 0, True)
        self.assertEqual(r["i2c"], "register_stub_no_error_observed")
        self.assertEqual(r["wifi_station"], "not_verified")
        self.assertEqual(r["tls_handshake"], "not_verified")

    def test_explicit_panic_is_evidence(self):
        r = runner.classify_log("Guru Meditation Error: Core 0 panic'ed", "c3", 1, False)
        self.assertTrue(r["markers"]["panic_detected"])

if __name__ == "__main__":
    unittest.main()
