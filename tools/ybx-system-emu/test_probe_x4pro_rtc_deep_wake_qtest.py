"""Host contracts for 0097: deep sleep wakes via a DIGITAL reset, never a returning IRQ."""
from pathlib import Path
import unittest
from probe_x4pro_rtc_deep_wake_qtest import (
    RTC, STATE0, RESET_CAUSE, INT_ENA, INT_RAW, INT_ST, SLEEP,
    DEEPSLEEP_RESET,
)

HERE = Path(__file__).resolve().parent
PATCH = HERE / 'experiments/0097-s3-rtc-explicit-deep-wakeup-dig-reset.patch'


class ExplicitDeepWakeContracts(unittest.TestCase):
    def test_s3_reset_cause_registers_and_masks(self):
        self.assertEqual(RTC, 0x60008000)
        self.assertEqual((STATE0, RESET_CAUSE, INT_ENA, INT_RAW, INT_ST),
                         (0x18, 0x38, 0x40, 0x44, 0x48))
        self.assertEqual((SLEEP, DEEPSLEEP_RESET), (1 << 31, 5))

    def test_deep_wake_is_host_triggered_and_requires_armed_sleep(self):
        content = PATCH.read_text()
        self.assertIn('ybx-rtc-inject-deep-wakeup', content)
        self.assertIn('!(s->state0_reg & R_RTC_CNTL_STATE0_SLEEP_EN_MASK)', content)
        self.assertIn('s->reset_cause[0] = ESP32_DEEPSLEEP_RESET;', content)
        self.assertIn('s->reset_cause[1] = ESP32_DEEPSLEEP_RESET;', content)
        self.assertIn('qemu_irq_pulse(s->dig_reset_req);', content)
        self.assertIn('s->rtc_int_raw &= ~RTC_CNTL_SLEEP_INT_MASK;', content)

    def test_no_firmware_or_other_device_modification(self):
        content = PATCH.read_text()
        self.assertEqual(content.count('--- a/'), 3)
        self.assertEqual(content.count('+++ b/'), 3)
        for forbidden in ('esp32s3_wifi_stub.c', 'ybx_ssd1677.c',
                          'netdev', 'factory-flash', 'test-sd-fat'):
            self.assertNotIn(forbidden, content)


if __name__ == '__main__':
    unittest.main()
