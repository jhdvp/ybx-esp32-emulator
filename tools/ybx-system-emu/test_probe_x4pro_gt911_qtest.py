"""Host contract tests for live GT911 GPIO/I2C timing probe."""
import unittest

from probe_x4pro_gt911_qtest import make_protocol, validate


class GT911ProbeTests(unittest.TestCase):
    def test_explicit_power_reset_and_virtual_clock(self):
        p = make_protocol()
        self.assertIn("writel 0x60004024 0x416", p.commands)
        self.assertIn("clock_step 3000000", p.commands)
        self.assertIn("clock_step 60000000", p.commands)
        self.assertIn("touch.product.byte0", p.read_names)
        self.assertIn("touch.alternate_address.byte2", p.read_names)
        self.assertIn("touch.master_rail_dropped.write_status", p.read_names)

    def test_reject_premature_ack(self):
        p = make_protocol()
        with self.assertRaises(AssertionError):
            validate(p, "\n".join(["OK 0x80"] * len(p.read_names)) + "\n")
