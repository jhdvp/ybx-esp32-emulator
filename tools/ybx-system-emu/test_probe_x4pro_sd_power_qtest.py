"""QTest host contracts for X4 Pro power-gated native SD slot."""
import unittest

from probe_x4pro_sd_power_qtest import scenario, parse


class SDPowerQtestContracts(unittest.TestCase):
    def test_powered_profile_sets_gpio1_and_gpio5(self):
        script, names = scenario(True)
        self.assertIn("writel 0x60004024 0x22", script)
        self.assertIn("writel 0x60004008 0x2", script)
        self.assertIn("writel 0x60004008 0x20", script)
        self.assertIn("writel 0x6000400c 0x20", script)
        self.assertIn("master_reenabled.irq", names)

    def test_generic_machine_has_no_board_gpio_ops(self):
        script, names = scenario(False)
        self.assertNotIn("0x60004024", script)
        self.assertEqual(names, ["initial.irq", "initial.r0"])

    def test_missing_qtest_reads_fail(self):
        _, names = scenario(True)
        with self.assertRaisesRegex(AssertionError, "readbacks expected"):
            parse("OK 0x0\n", names)
