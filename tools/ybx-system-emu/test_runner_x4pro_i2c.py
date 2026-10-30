"""Verify X4 Pro optional I2C profile is explicit and traceable."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from run_open_qemu import run_one

class X4ProRunnerTests(TestCase):
    def make_flash_rom(self, root, chip):
        image = root / "factory.bin"
        data = bytearray(b"\xff" * 0x10100)
        data[0] = 0xe9
        data[0x8000:0x8002] = bytes((0xaa, 0x50))
        data[0x10000] = 0xe9
        image.write_bytes(data)
        qemu = root / "qemu"
        (qemu / "build-ybx").mkdir(parents=True)
        (qemu / "build-ybx" /
            ("esp32s3_rev0_rom.bin" if chip == "s3"
             else "esp32c3-rom.bin")).write_bytes(bytes(0x60000))
        return image, qemu

    def test_explicit_x4pro_s3_profile(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image, qemu = self.make_flash_rom(root, "s3")
            with patch("run_open_qemu.subprocess.run",
                       return_value=SimpleNamespace(
                           stdout="", stderr="", returncode=124)) as proc:
                report = run_one("s3", image, qemu, root / "logs", 2,
                                 None, "test-builder", x4pro_i2c=True)
            command = proc.call_args.args[0]
            self.assertEqual(command[command.index("-machine") + 1],
                             "esp32s3,ybx-x4pro-i2c=on")
            self.assertTrue(report["virtual_x4pro_i2c_attached"])
            self.assertFalse(report["virtual_x4pro_gt911_attached"])
            self.assertFalse(report["guest_driver_messages"])

    def test_c3_rejects_x4pro_bus(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image, qemu = self.make_flash_rom(root, "c3")
            with self.assertRaisesRegex(ValueError, "ESP32-S3"):
                run_one("c3", image, qemu, root / "logs", 2,
                        None, "test-builder", x4pro_i2c=True)


    def test_gt911_optin_in_machine_does_not_claim_real_input(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image, qemu = self.make_flash_rom(root, "s3")
            with patch("run_open_qemu.subprocess.run",
                       return_value=SimpleNamespace(
                           stdout="", stderr="", returncode=124)) as proc:
                report = run_one("s3", image, qemu, root / "logs", 2,
                                 None, "test-builder", x4pro_i2c=True,
                                 x4pro_gt911=True)
            command = proc.call_args.args[0]
            self.assertEqual(command[command.index("-machine") + 1],
                "esp32s3,ybx-x4pro-i2c=on,ybx-x4pro-gt911=on")
            self.assertTrue(report["virtual_x4pro_gt911_attached"])
            self.assertFalse(report["gt911_input_verified"])
            self.assertFalse(report["epaper_verified"])

    def test_gt911_without_i2c_fails_closed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image, qemu = self.make_flash_rom(root, "s3")
            with self.assertRaisesRegex(ValueError, "requires"):
                run_one("s3", image, qemu, root / "logs", 2,
                        None, "test-builder", x4pro_gt911=True)


    def test_sd_power_gate_optin_and_strict_status(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image, qemu = self.make_flash_rom(root, "s3")
            with patch("run_open_qemu.subprocess.run",
                       return_value=SimpleNamespace(
                           stdout="", stderr="", returncode=124)) as proc:
                report = run_one("s3", image, qemu, root / "logs", 2,
                                 None, "test-builder", x4pro_sd_power=True)
            command = proc.call_args.args[0]
            self.assertEqual(command[command.index("-machine") + 1],
                             "esp32s3,ybx-x4pro-sd-power=on")
            self.assertTrue(report["virtual_x4pro_sd_power_gate"])
            self.assertFalse(report["guest_sd_power_cycle_verified"])
            self.assertFalse(report["sd_mount_verified"])

    def test_sd_power_gate_c3_rejected(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image, qemu = self.make_flash_rom(root, "c3")
            with self.assertRaisesRegex(ValueError, "ESP32-S3"):
                run_one("c3", image, qemu, root / "logs", 2,
                        None, "test-builder", x4pro_sd_power=True)


    def make_fat(self, root):
        sd = root / "test-sd.img"
        content = bytearray(512)
        content[54:62] = b"FAT16   "
        content[11:13] = (512).to_bytes(2, "little")
        content[13] = 4
        content[510:512] = b"\x55\xaa"
        sd.write_bytes(content)
        return sd

    def test_realtime_s3_guest_fat_evidence_must_come_from_analysis(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image, qemu = self.make_flash_rom(root, "s3")
            sd = self.make_fat(root)
            with (patch("run_open_qemu.subprocess.run",
                        return_value=SimpleNamespace(
                            stdout="ESP-ROM:esp32s3-20210327\n"
                                   "boot:0x4 (SPI_FLASH_BOOT)\n"
                                   "entry 0x403c8898\nPreferences.cpp:47",
                            stderr="", returncode=124)) as proc,
                  patch("analyze_guest_fat_image.analyze",
                        return_value={
                            "guest_sd_fat_file_creation_verified": True,
                            "guest_written_fat_structure_verified": True
                        }) as analyzer):
                report = run_one("s3", image, qemu, root / "logs", 2,
                                 sd, "test-builder", s3_realtime=True,
                                 sd_original_fat=sd)
            command = proc.call_args.args[0]
            self.assertNotIn("-icount", command)
            self.assertTrue(report["guest_driver_messages"])
            self.assertTrue(report["sd_mount_verified"])
            self.assertTrue(report["guest_fat_file_created"])
            self.assertFalse(report["epaper_verified"])
            analyzer.assert_called_once()

    def test_realtime_s3_rejects_missing_guest_fat_artifacts(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image, qemu = self.make_flash_rom(root, "s3")
            sd = self.make_fat(root)
            with (patch("run_open_qemu.subprocess.run",
                        return_value=SimpleNamespace(
                            stdout="", stderr="", returncode=124)),
                  patch("analyze_guest_fat_image.analyze",
                        side_effect=ValueError("no guest FAT file"))):
                report = run_one("s3", image, qemu, root / "logs", 2,
                                 sd, "test-builder", s3_realtime=True,
                                 sd_original_fat=sd)
            self.assertFalse(report["sd_mount_verified"])
            self.assertFalse(report["guest_fat_file_created"])
            self.assertIn("no guest FAT file", report["guest_fat_evidence_error"])

    def test_guest_fat_baseline_cannot_be_used_on_c3(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image, qemu = self.make_flash_rom(root, "c3")
            sd = self.make_fat(root)
            with self.assertRaisesRegex(ValueError, "requires S3"):
                run_one("c3", image, qemu, root / "logs", 2,
                        sd, "test-builder", sd_original_fat=sd)


    def test_spi2_optin_is_pio_only_and_does_not_claim_epaper(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image, qemu = self.make_flash_rom(root, "s3")
            with patch("run_open_qemu.subprocess.run",
                       return_value=SimpleNamespace(
                           stdout="", stderr="", returncode=124)) as proc:
                report = run_one("s3", image, qemu, root / "logs", 2,
                                 None, "test-builder", x4pro_spi2=True)
            cmd = proc.call_args.args[0]
            self.assertIn("esp32s3,ybx-x4pro-spi2=on", cmd)
            self.assertTrue(report["virtual_x4pro_spi2_pio"])
            self.assertFalse(report["epd_panel_transport_verified"])
            self.assertFalse(report["epaper_verified"])

    def test_spi2_optin_rejected_on_c3(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image, qemu = self.make_flash_rom(root, "c3")
            with self.assertRaisesRegex(ValueError, "ESP32-S3"):
                run_one("c3", image, qemu, root / "logs", 2,
                        None, "test-builder", x4pro_spi2=True)


    def test_ssd1677_optin_keeps_epaper_unverified_by_uart(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image, qemu = self.make_flash_rom(root, "s3")
            with patch("run_open_qemu.subprocess.run",
                       return_value=SimpleNamespace(
                           stdout="ESP-ROM:esp32s3\nentry 0x403c8898\n",
                           stderr="", returncode=124)) as proc:
                report = run_one("s3", image, qemu, root / "logs", 2,
                                 None, "test-builder", x4pro_spi2=True,
                                 x4pro_epd=True)
            cmd = proc.call_args.args[0]
            self.assertIn("ybx-x4pro-spi2=on,ybx-x4pro-epd=on",
                          cmd[cmd.index("-machine")+1])
            self.assertTrue(report["virtual_x4pro_epd_ssd1677_attached"])
            self.assertFalse(report["guest_epd_frame_verified"])
            self.assertFalse(report["epaper_verified"])

    def test_ssd1677_requires_spi2_and_s3(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image, qemu = self.make_flash_rom(root, "s3")
            with self.assertRaisesRegex(ValueError, "--x4pro-spi2"):
                run_one("s3", image, qemu, root / "logs", 2,
                        None, "test-builder", x4pro_epd=True)
            (root / "c3").mkdir()
            image, qemu = self.make_flash_rom(root / "c3", "c3")
            with self.assertRaisesRegex(ValueError, "requires ESP32-S3"):
                run_one("c3", image, qemu, root / "logs", 2,
                        None, "test-builder", x4pro_spi2=True,
                        x4pro_epd=True)
