"""Host contract tests for real SDMMC command and disposable DMA probes."""
import unittest

from probe_x4pro_sdmmc_qtest import BASE, commands, parse
from probe_x4pro_sdmmc_sector_qtest import DRAM_BUFFER, DRAM_DESC


class SDMMCProbeTests(unittest.TestCase):
    def test_controller_and_card_command_registers(self):
        script, names = commands()
        self.assertEqual(BASE, 0x60028000)
        self.assertIn("writel 0x6002802c 0x80000000", script)
        self.assertIn("writel 0x6002802c 0x80000148", script)
        self.assertIn("cmd8_response", names)
        self.assertIn("block_size_configured", names)

    def test_reject_truncated_qtest_replies(self):
        _, names = commands()
        with self.assertRaisesRegex(AssertionError, "length mismatch"):
            parse("OK 0x4\n", names)

    def test_sector_dma_targets_are_guest_dram_not_host_paths(self):
        self.assertEqual(DRAM_DESC, 0x3fc80000)
        self.assertEqual(DRAM_BUFFER, 0x3fc81000)
