"""Host contracts for research-only X4Pro POWER GPIO3 -> RTCIO3 model.

Real acceptance runs QTest on compiled QEMU and original guest flash; this
host suite checks source/probe invariants without requiring local QEMU.
"""
from pathlib import Path
import unittest
from probe_x4pro_power_button_rtc_qtest import (
    RTC, GPIO, STATE0, WAKEUP, CAUSE, CONF, SEL, STATUS, SLEEP, EXT1,
)

PATCH = Path(__file__).parent / 'experiments/0101-x4pro-power-button-gpio3-rtc3-reset-retention.patch'


class X4ProPowerRtcContracts(unittest.TestCase):
    def test_real_s3_gpio3_and_ext1_registers(self):
        self.assertEqual(GPIO, 0x60004000)
        self.assertEqual(RTC, 0x60008000)
        self.assertEqual((STATE0, WAKEUP, CAUSE), (0x18, 0x3c, 0x38))
        self.assertEqual((CONF, SEL, STATUS), (0x64, 0xe0, 0xe4))
        self.assertEqual((SLEEP, EXT1), (1 << 31, 1 << 16))

    def test_no_direct_reset_or_unconditional_wake(self):
        content = PATCH.read_text()
        self.assertIn('ybx-x4pro-power-button-pressed', content)
        self.assertIn('"ybx.gpio-input", 3', content)
        self.assertIn('ESP32S3_RTC_EXT1_PAD_IN, 3', content)
        self.assertIn('pressed ? 0 : 1', content)
        self.assertNotIn('qemu_system_reset_request', content)
        self.assertNotIn('ESP32_DEEPSLEEP_RESET', content)

    def test_digital_reset_preserves_external_voltage(self):
        content = PATCH.read_text()
        self.assertIn('external_known_lo', content)
        self.assertIn('external_high_lo', content)
        self.assertIn('input_pullup_lo & ~s->external_known_lo', content)
        self.assertIn('s->external_high_hi & s->external_known_hi', content)

    def test_only_board_optin_exposes_power_button(self):
        content = PATCH.read_text()
        self.assertIn('if (!s->ybx_x4pro_gt911)', content)
        self.assertIn('X4 Pro power button requires the X4 Pro board profile', content)
        self.assertNotIn('esp32s3_wifi_stub.c', content)
        self.assertNotIn('ybx_x4pro_i2c.c', content)
        self.assertEqual(content.count('--- a/'), 3)


if __name__ == '__main__':
    unittest.main()
