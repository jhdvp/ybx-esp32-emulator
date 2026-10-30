"""Portable contract checks for experimental C3 ADC QTest script."""
import unittest
from probe_c3_adc_qtest import protocol,validate

class C3ADCProbeTests(unittest.TestCase):
    def test_no_premature_adc_done(self):
        commands, expected=protocol()
        self.assertIn("writel 0x60040020 0x80000000", commands)
        self.assertIn("writel 0x60040020 0xa0000000", commands)
        self.assertEqual(expected,[0,0x80000000,0,0])
    def test_accepted_adc_handshake(self):
        result=validate("OK\nOK 0x0\nOK\nOK 0x80000000\nOK 0x0\nOK\nOK 0x0\n")
        self.assertTrue(result["done_cleared"])
        self.assertFalse(result["genuine_sensor_voltage_verified"])
    def test_reject_fake_premature_done(self):
        with self.assertRaisesRegex(RuntimeError,"mismatch"):
            validate("OK\nOK 0x80000000\nOK\nOK 0x80000000\nOK 0x0\nOK\nOK 0x0\n")
if __name__=="__main__":
    unittest.main()
