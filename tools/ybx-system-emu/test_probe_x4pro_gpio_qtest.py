"""Contract tests for optional ESP32-S3 GPIO OUT/ENABLE QTest."""
import unittest
from probe_x4pro_gpio_qtest import protocol, validate

class GPIOProbeTests(unittest.TestCase):
    def test_register_writes_and_high_bank(self):
        script=protocol()
        self.assertIn("writel 0x60004024 0x414",script)
        self.assertIn("writel 0x6000400c 0x4",script)
        self.assertIn("writel 0x60004030 0x2",script)

    def test_negative_default_profile(self):
        values=[4,0,0,0,0,0,0,0,0,0]
        response="\n".join(f"OK 0x{n:x}" for n in values)+"\n"
        self.assertFalse(validate(response,False)["gpio_output_mmio_enabled"])

    def test_incorrect_gpio_disabled_readback_fails(self):
        response="\n".join(["OK 0x0"]*10)+"\n"
        with self.assertRaisesRegex(RuntimeError,"GPIO"):
            validate(response,False)
