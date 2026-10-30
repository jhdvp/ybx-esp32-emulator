"""Unit tests for QTest I2C NACK contract, without executing QEMU."""
import unittest

from probe_i2c_qtest import protocol, validate_responses

class I2CQTestProtocolTests(unittest.TestCase):
    def test_modern_command_and_absent_slave_address(self):
        text, expected = protocol()
        self.assertIn("writel 0x6001301c 0xba", text)
        self.assertIn("writel 0x60013058 0x901", text)
        self.assertIn("writel 0x6001305c 0x1000", text)
        self.assertEqual(expected, [0x400, 0x400, 0x80000901, 0,
                                    0x80003000, 0x80001000, 0x80])

    def test_qtest_expected_nack_and_clear(self):
        responses = "OK\nOK\nOK\nOK\nOK\nOK\nOK\n"
        responses += "OK 0x00000400\nOK 0x00000400\nOK 0x80000901\n"
        responses += "OK\nOK 0x00000000\n"
        responses += "OK\nOK\nOK\n"
        responses += "OK 0x80003000\nOK 0x80001000\nOK 0x00000080\n"
        result = validate_responses(responses)
        self.assertTrue(result["first_command_done"])
        self.assertTrue(result["nack_cleared"])
        self.assertTrue(result["modern_restart_done"])
        self.assertTrue(result["modern_stop_done"])
        self.assertTrue(result["stop_interrupt"])

    def test_qtest_rejects_fake_success(self):
        with self.assertRaisesRegex(RuntimeError, "mismatch"):
            validate_responses("OK 0x0\nOK 0x0\nOK 0x80000901\nOK 0x0\n")

if __name__ == "__main__":
    unittest.main()
