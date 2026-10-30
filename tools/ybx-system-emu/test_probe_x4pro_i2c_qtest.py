"""Host-only regression checks for live X4 Pro I2C QTest instructions."""
import unittest
from probe_x4pro_i2c_qtest import make_protocol, decode

class X4ProI2CTests(unittest.TestCase):
    def test_explicit_profile_and_80_byte_battery_upload(self):
        p = make_protocol(True)
        self.assertEqual(sum("gauge.profile_chunk_" in x
                             for x in p.read_names), 8)
        self.assertIn("writel 0x6001301c 0xc6", p.commands)
        self.assertIn("writel 0x6001301c 0x5f", p.commands)
        self.assertIn("writel 0x60013060 0x1000", p.commands)
        self.assertGreater(len(p.commands), 200)

    def test_without_profile_always_nacks(self):
        p = make_protocol(False)
        self.assertEqual(len(p.read_names), 6)
        responses = []
        for key in p.read_names:
            v = 0x400 if key.endswith("status") else 0xee
            responses.append(f"OK 0x{v:x}")
        report = decode(p, "\n".join(responses) + "\n", False)
        self.assertFalse(report["rtc_register_readback"])

    def test_rejects_unexpected_ack_when_disabled(self):
        p = make_protocol(False)
        with self.assertRaises(AssertionError):
            decode(p, "\n".join(["OK 0x80"] * 6) + "\n", False)

if __name__ == "__main__":
    unittest.main()
