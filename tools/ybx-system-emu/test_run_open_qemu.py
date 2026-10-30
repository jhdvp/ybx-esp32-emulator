"""Isolated acceptance-gate tests for open Espressif QEMU diagnostics."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from run_open_qemu import classify_qemu_log, timeout_text, validate_sd_image


class QemuEvidenceTests(unittest.TestCase):
    def test_gdma_crash_is_not_a_timeout_or_boot(self):
        log = ("Adding SPI flash device\n"
               "esp32c3_gdma.c:107: esp32c3_generic_int_reg: "
               "Assertion 'word < DIM(assoc)' failed.")
        report = classify_qemu_log(log, 134)
        self.assertTrue(report["gdma_assertion"])
        self.assertTrue(report["qemu_abort"])
        self.assertFalse(report["rom_seen"])
        self.assertFalse(report["qemu_timeout"])
        self.assertEqual(report["boot_evidence"], "no_guest_rom_uart")

    def test_usb_serial_rom_and_app_panic_are_separate_gates(self):
        log = ("ESP-ROM:esp32s3-20210327\\n"
               "rst:0x1 (POWERON),boot:0x4 (SPI_FLASH_BOOT)\\n"
               "entry 0x403c8898\\n"
               "E esp_psram: initialization failed\\n"
               "Guru Meditation Error: Core 0 panic'ed (LoadProhibited)")
        result = classify_qemu_log(log, 124)
        self.assertTrue(result["rom_seen"])
        self.assertTrue(result["spi_flash_boot_seen"])
        self.assertEqual(result["boot_evidence"], "application_uart")
        self.assertTrue(result["guest_panic_detected"])
        self.assertTrue(result["qemu_timeout"])

    def test_qemu_timeout_without_uart_is_not_boot(self):
        report = classify_qemu_log("Adding SPI flash device", 124)
        self.assertTrue(report["qemu_timeout"])
        self.assertFalse(report["qemu_abort"])
        self.assertEqual(report["boot_evidence"], "no_guest_rom_uart")

    def test_rom_uart_is_not_application_start(self):
        report = classify_qemu_log(
            "ESP-ROM:esp32c3\nSPI_FAST_FLASH_BOOT\n", 124)
        self.assertEqual(report["boot_evidence"], "rom_uart")
        self.assertFalse(report["guest_driver_messages"])

    def test_application_uart_is_separate_from_peripherals(self):
        report = classify_qemu_log(
            "ESP-ROM:esp32c3\nSPI_FAST_FLASH_BOOT\nentry 0x403cbf10\n"
            "Preferences.cpp: NVS missing", 124)
        self.assertEqual(report["boot_evidence"], "application_uart")

    def test_timeout_stream_handles_bytes_and_str(self):
        self.assertEqual(timeout_text(None), "")
        self.assertEqual(timeout_text(bytes((98, 97, 100, 255))), "bad" + chr(0xfffd))
        self.assertEqual(timeout_text("done"), "done")


class SdValidationTests(unittest.TestCase):
    def make_sd(self, folder: str, filesystem: str) -> Path:
        boot = bytearray(512)
        boot[11:13] = (512).to_bytes(2, "little")
        boot[13] = 4
        offset = 54 if filesystem == "FAT16" else 82
        boot[offset:offset + 5] = filesystem.encode()
        boot[510:512] = bytes((0x55, 0xAA))
        path = Path(folder) / "test-sd.img"
        path.write_bytes(boot)
        return path

    def test_fat16_superfloppy(self):
        with TemporaryDirectory() as temp:
            validate_sd_image(self.make_sd(temp, "FAT16"))

    def test_fat32_superfloppy(self):
        with TemporaryDirectory() as temp:
            validate_sd_image(self.make_sd(temp, "FAT32"))

    def test_valid_mbr_fat16_first_partition(self):
        with TemporaryDirectory() as temp:
            raw = self.make_sd(temp, "FAT16").read_bytes()
            disk = bytearray(4096)
            disk[510:512] = bytes((0x55, 0xaa))
            disk[446] = 0x00
            disk[450] = 0x06
            disk[454:458] = (1).to_bytes(4, "little")
            disk[458:462] = (1).to_bytes(4, "little")
            disk[512:1024] = raw
            image = Path(temp) / "mbr.img"
            image.write_bytes(disk)
            validate_sd_image(image)

    def test_rejects_fat_part_outside_image(self):
        with TemporaryDirectory() as temp:
            raw = self.make_sd(temp, "FAT16").read_bytes()
            disk = bytearray(4096)
            disk[510:512] = bytes((0x55, 0xaa))
            disk[450] = 0x06
            disk[454:458] = (9).to_bytes(4, "little")
            disk[458:462] = (64).to_bytes(4, "little")
            disk[512:1024] = raw
            image = Path(temp) / "invalid-mbr.img"
            image.write_bytes(disk)
            with self.assertRaisesRegex(ValueError, "boot sector"):
                validate_sd_image(image)

    def test_rejects_marker_without_boot_signature(self):
        with TemporaryDirectory() as temp:
            image = self.make_sd(temp, "FAT16")
            data = bytearray(image.read_bytes())
            data[510:512] = bytes((0, 0))
            image.write_bytes(data)
            with self.assertRaisesRegex(ValueError, "boot sector"):
                validate_sd_image(image)

    def test_rejects_unformatted_sd(self):
        with TemporaryDirectory() as temp:
            image = Path(temp) / "empty.img"
            image.write_bytes(bytes(512))
            with self.assertRaisesRegex(ValueError, "boot sector"):
                validate_sd_image(image)



class CustomRomRunnerTests(unittest.TestCase):
    def test_custom_s3_rom_is_isolated_and_hashed(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import hashlib
        from run_open_qemu import run_one

        with TemporaryDirectory() as temp:
            base = Path(temp)
            firmware = base / "factory.bin"
            flash = bytearray(bytes((255,)) * 0x10100)
            flash[0] = 0xe9
            flash[0x8000:0x8002] = bytes((0xaa, 0x50))
            flash[0x10000] = 0xe9
            firmware.write_bytes(flash)
            qemu = base / "qemu"
            (qemu / "build-ybx").mkdir(parents=True)
            (qemu / "build-ybx/esp32s3_rev0_rom.bin").write_bytes(
                bytes(0x60000))
            custom = base / "custom-rom.bin"
            custom.write_bytes(bytes((35,)) * 0x60000)
            output = base / "reports"

            with patch("run_open_qemu.subprocess.run",
                       return_value=SimpleNamespace(stdout="", stderr="",
                                                     returncode=124)) as mock:
                result = run_one("s3", firmware, qemu, output, 2,
                                 None, "test-builder", custom)

            command = mock.call_args.args[0]
            self.assertEqual(command[command.index("-L") + 1], "/work/bios")
            self.assertEqual(result["rom_source"], str(custom))
            self.assertEqual(result["rom_sha256"],
                             hashlib.sha256(custom.read_bytes()).hexdigest())
            self.assertEqual(result["runtime_exit_code"], 124)
            self.assertFalse(result["guest_driver_messages"])
            self.assertTrue(result["qemu_timeout"])

    def test_c3_custom_rom_mounts_expected_bios(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from run_open_qemu import run_one
        with TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "factory.bin"
            factory = bytearray(bytes((255,)) * 0x10100)
            factory[0] = 0xe9
            factory[0x8000:0x8002] = bytes((0xaa, 0x50))
            factory[0x10000] = 0xe9
            source.write_bytes(factory)
            qemu = base / "qemu"
            (qemu / "build-ybx").mkdir(parents=True)
            (qemu / "build-ybx/esp32c3-rom.bin").write_bytes(bytes(0x60000))
            custom = base / "custom-c3.rom"
            custom.write_bytes(bytes((7,)) * 0x60000)
            with patch("run_open_qemu.subprocess.run",
                       return_value=SimpleNamespace(stdout="", stderr="",
                                                     returncode=124)) as mock:
                evidence = run_one("c3", source, qemu, base / "logs", 2,
                                   None, "test-builder", None, True, custom)
            command = mock.call_args.args[0]
            self.assertEqual(command[command.index("-L") + 1], "/work/bios")
            self.assertEqual(evidence["rom_source"], str(custom))
            self.assertTrue(evidence["usb_serial_routed"])
            self.assertEqual(command[-6:], ["-serial", "null",
                                            "-serial", "null",
                                            "-serial", "stdio"])

    def test_s3_octal_psram_command_is_explicit_and_auditable(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from run_open_qemu import run_one
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            image = base / "factory.bin"
            flash = bytearray(bytes((255,)) * 0x10100)
            flash[0] = 0xe9
            flash[0x8000:0x8002] = bytes((0xaa, 0x50))
            flash[0x10000] = 0xe9
            image.write_bytes(flash)
            qemu = base / "qemu"
            (qemu / "build-ybx").mkdir(parents=True)
            (qemu / "build-ybx/esp32s3_rev0_rom.bin").write_bytes(
                bytes(0x60000))
            with patch("run_open_qemu.subprocess.run",
                       return_value=SimpleNamespace(stdout="", stderr="",
                                                     returncode=124)) as mock:
                result = run_one("s3", image, qemu, base / "logs", 2,
                                 None, "builder", None, True, None, 8)
            command = mock.call_args.args[0]
            self.assertEqual(command[command.index("-m") + 1], "8M")
            self.assertEqual(command[command.index("-global") + 1],
                             "ssi_psram.is_octal=on")
            self.assertEqual(result["virtual_octal_psram_mb"], 8)
            self.assertTrue(result["usb_serial_routed"])

    def test_wrong_s3_rom_size_rejected(self):
        from run_open_qemu import run_one
        with TemporaryDirectory() as temp:
            base = Path(temp)
            flash = bytearray(bytes((255,)) * 0x10100)
            flash[0] = 0xe9
            flash[0x8000:0x8002] = bytes((0xaa, 0x50))
            flash[0x10000] = 0xe9
            firmware = base / "factory.bin"
            firmware.write_bytes(flash)
            qemu = base / "qemu"
            (qemu / "build-ybx").mkdir(parents=True)
            (qemu / "build-ybx/esp32s3_rev0_rom.bin").write_bytes(
                bytes(0x60000))
            wrong_rom = base / "truncated.bin"
            wrong_rom.write_bytes(bytes(0x100))
            with self.assertRaisesRegex(ValueError, "unexpected ROM size"):
                run_one("s3", firmware, qemu, base / "report", 2,
                        None, "test-builder", wrong_rom)


if __name__ == "__main__":
    unittest.main()
