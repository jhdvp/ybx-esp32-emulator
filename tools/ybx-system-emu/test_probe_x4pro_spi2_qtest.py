"""Host contract tests for optional GP-SPI2 and explicit limitations."""
import unittest
from probe_x4pro_spi2_qtest import BASE, MMIO

class GP_SPI2QTestContracts(unittest.TestCase):
    def test_esp_idf_s3_gpspi2_offsets(self):
        self.assertEqual(BASE, 0x60024000)
        self.assertEqual(MMIO["cmd"], 0)
        self.assertEqual(MMIO["user"], 0x10)
        self.assertEqual(MMIO["user2"], 0x18)
        self.assertEqual(MMIO["ms_dlen"], 0x1c)
        self.assertEqual(MMIO["w0"], 0x98)
        self.assertNotEqual(BASE, 0x60002000)
