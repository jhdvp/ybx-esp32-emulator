"""Contract tests for native QEMU11 Power GPIO3 and automatic release."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import unittest

from serve_einkqemu11_browser import Qemu11


class PowerWatchdogTests(unittest.TestCase):
    def make_stub(self, enabled=True):
        engine = object.__new__(Qemu11)
        engine.args = SimpleNamespace(enable_power=enabled)
        engine.lock = threading.RLock()
        engine.power_pressed = False
        engine.power_generation = 0
        engine.power_timer = None
        engine.qmp = Mock(return_value={})
        return engine

    def test_default_denied(self):
        engine = self.make_stub(False)
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            engine.set_power(True)
        engine.qmp.assert_not_called()

    def test_strict_boolean(self):
        engine = self.make_stub()
        for bad in (1, 0, None, "true", {}):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    engine.set_power(bad)
        engine.qmp.assert_not_called()

    def test_native_press_release(self):
        engine = self.make_stub()
        try:
            self.assertTrue(engine.set_power(True)["pressed"])
            self.assertFalse(engine.set_power(False)["pressed"])
            requests = [call.args[1] for call in engine.qmp.call_args_list]
            self.assertEqual([x["value"] for x in requests], [True, False])
            self.assertTrue(all(x["path"] == "/machine/x4pro-keys"
                                and x["property"] == "power" for x in requests))
        finally:
            if engine.power_timer:
                engine.power_timer.cancel()

    def test_stale_watchdog_cannot_release(self):
        engine = self.make_stub()
        try:
            engine.set_power(True)
            gen = engine.power_generation
            engine._auto_release_power(gen - 1)
            self.assertTrue(engine.power_pressed)
            engine._auto_release_power(gen)
            self.assertFalse(engine.power_pressed)
            self.assertEqual(engine.qmp.call_count, 2)
        finally:
            if engine.power_timer:
                engine.power_timer.cancel()

    def test_duplicate_press_does_not_extend_deadline(self):
        engine = self.make_stub()
        try:
            engine.set_power(True)
            timer = engine.power_timer
            engine.set_power(True)
            self.assertIs(timer, engine.power_timer)
            self.assertEqual(engine.qmp.call_count, 1)
        finally:
            engine.set_power(False)


if __name__ == "__main__":
    unittest.main()
