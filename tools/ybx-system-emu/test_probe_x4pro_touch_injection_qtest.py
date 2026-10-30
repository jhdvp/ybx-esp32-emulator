"""Host-only smoke checks for QTest/QMP synthetic GT911 input harness."""
import io
from types import SimpleNamespace
import unittest
from probe_x4pro_touch_injection_qtest import QTest, I2C, GPIO


class SyntheticTouchProbeTests(unittest.TestCase):
    def test_qtest_gpio_i2c_commands_are_real_mmio(self):
        responses = "OK\nOK 0x0000000000000080\nOK 3000000\n"
        p = SimpleNamespace(
            stdin=io.StringIO(), stdout=io.StringIO(responses))
        q = QTest(p)
        q.wr(GPIO + 0x24, 0x416)
        self.assertEqual(q.rd(I2C + 0x20), 0x80)
        self.assertEqual(q.tick(3000000), 3000000)
        self.assertIn("writel 0x60004024 0x416", p.stdin.getvalue())
        self.assertIn("clock_step 3000000", p.stdin.getvalue())
        self.assertEqual(q.commands, 3)

    def test_qtest_rejects_fail_reply(self):
        p = SimpleNamespace(
            stdin=io.StringIO(), stdout=io.StringIO("FAIL bad command\n"))
        with self.assertRaisesRegex(AssertionError, "FAIL"):
            QTest(p).raw("clock_step 3000000")
