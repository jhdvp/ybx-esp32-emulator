#!/usr/bin/env python3
"""Host-only fidelity model for the SSD1677 waveform/sleep/reset gaps (0061).

Two Python mirrors share this file and nothing else:
  * Panel        — line-for-line mirror of hw/display/ybx_ssd1677.c as
    accepted in the build (chain 0042+0043+0037+0051), audit checkout
    qemu-0043-agent-audit-20261030: epd_set_busy:15-35 (edge telemetry,
    unconditional drive), epd_refresh_completed:36-39, epd_clear_ram:40-54,
    epd_reset_gpio:55-65 (RESET falling = timer_del + release + clear RAM,
    rising branch empty), epd_apply_params:71-115, epd_expected_params:116-128
    (no 0x10 entry), epd_advance_ram:129-148, epd_transfer:149-218 (no 0x10
    branch; 0x20 arms `waveform ? busy_waveform_ms : busy_power_ms` at
    :184-186), epd_reset:231-249 (0051 clear-after-release ordering),
    epd_properties:294-298 (busy-waveform-ms=350, busy-power-ms=350).
  * PatchedPanel — mirror of experiments/0061-ssd1677-waveform-sleep.patch on
    top: per-category BUSY accounting (busy_power/waveform/reset_high_ms +
    busy_reason armed with each window), a deep-sleep state entered by 0x10
    (all later bytes rejected until a hardware RESET; SW_RESET 0x12 does not
    wake), and an opt-in post-reset BUSY window behind busy-on-reset-ms
    (default 0 keeps the accepted behaviour exactly).

Guest contract that motivates each gap (fw-092c2be5 FreeInk SDK):
  EpdBus.cpp:202-230           reset() = HIGH 10 / LOW 10 / HIGH 10 ms pulse
  Ssd1677Driver.cpp:381-390    initController: SWRESET + fixed delay(10)
                               ("Active-high BUSY may not have asserted by the
                               first GPIO sample, so waitBusy() alone is not a
                               substitute for this delay")
  Ssd1677Driver.cpp:614-626    powerOffController: 0x22 0x03, 0x20, waitBusy
  Ssd1677Driver.cpp:956-966    deepSleep: powerOffController, 0x10, data 0x03

Each of the three recorded gaps (experiments/0048-batch2-epd-lifecycle.md §3)
is characterised by a test against the *accepted* mirror — it must fail on the
fidelity invariant — and the same scenario passes against the patched mirror.
No QEMU is run here; probe_x4pro_epd_waveform_qtest.py is the compiled-binary
counterpart for the orchestrator.
"""
from __future__ import annotations

import unittest

WIDTH, HEIGHT, ROW_BYTES = 800, 480, 100
SIZE = ROW_BYTES * HEIGHT
DISP_START = 0x04
SHIPPED_WINDOW_MS = 350  # ybx_ssd1677.c:295-296, both defaults, by construction


def expected_params(cmd):
    """epd_expected_params:116-128 — note there is no 0x10 entry."""
    return {0x11: 1, 0x44: 4, 0x45: 4, 0x4e: 2, 0x4f: 2, 0x22: 1, 0x46: 1}.get(cmd, 0)


class Panel:
    """Accepted ybx_ssd1677.c, line for line, single refresh_timer."""

    def __init__(self, waveform_ms=SHIPPED_WINDOW_MS, power_ms=SHIPPED_WINDOW_MS):
        self.waveform_ms, self.power_ms = waveform_ms, power_ms
        self.now = 0
        self._pending = None                      # timer_mod/timer_del stand-in
        self.ram = bytearray(b"\xff" * SIZE)
        self.visible = bytearray(b"\xff" * SIZE)
        self.frames = self.commands = self.data_bytes = self.ram_writes = 0
        self.ignored_bytes = self.last_command = self.nonwhite_visible = 0
        self.nonwhite_ram_writes = self.cmd24 = self.cmd26 = 0
        self.nonwhite_ram = 0
        self.update_ctrl2 = 0
        self.busy_level = 0
        self.busy_rise_edges = self.busy_fall_edges = self.busy_high_ms = 0
        self.busy_started_ms = 0
        self.command = 0
        self.params = [0] * 5
        self.param_len = 0
        self.data_entry = 0x01
        self.x_start, self.x_end = 0, WIDTH - 1
        self.y_start, self.y_end = HEIGHT - 1, 0
        self.x, self.y = 0, HEIGHT - 1
        self.dc_high = True
        self.rst_high = True

    # --- virtual clock -----------------------------------------------------
    def advance(self, ms):
        """QEMU_CLOCK_VIRTUAL timer list: one armed window, fired in order."""
        end = self.now + ms
        while self._pending is not None and self._pending <= end:
            self.now = self._pending
            self._pending = None
            self.refresh_completed()              # epd_refresh_completed:36-39
        self.now = end

    def refresh_completed(self):
        self.set_busy(False)

    # --- epd_set_busy:15-35 --------------------------------------------------
    def set_busy(self, busy):
        was_high = self.busy_level != 0
        now = self.now
        if busy != was_high:
            if busy:
                self.busy_rise_edges += 1
                self.busy_started_ms = now
            elif now > self.busy_started_ms:
                self.busy_fall_edges += 1
                self.busy_high_ms += now - self.busy_started_ms
        self.busy_level = int(busy)               # drives unconditionally

    # --- epd_clear_ram:40-54 ---------------------------------------------------
    def clear_ram(self):
        self.ram = bytearray(b"\xff" * SIZE)
        self.nonwhite_ram = 0
        self.x_start, self.x_end = 0, WIDTH - 1
        self.y_start, self.y_end = HEIGHT - 1, 0
        self.x, self.y = 0, HEIGHT - 1
        self.data_entry = 0x01
        self.param_len = 0

    # --- epd_transfer:149-218 --------------------------------------------------
    def transfer(self, byte, dc):
        self.dc_high = bool(dc)                   # epd_dc_gpio:66-70 precedes
        if not self.rst_high:                     # :153-156
            self.ignored_bytes += 1
            return
        if not self.dc_high:                      # :157-189 command path
            self.command = byte
            self.last_command = byte
            self.commands += 1
            if byte == 0x24:
                self.cmd24 += 1
            if byte == 0x26:
                self.cmd26 += 1
            self.param_len = 0
            if byte == 0x12:                      # :164-167 SW_RESET
                self.clear_ram()
                self._pending = None
                self.set_busy(False)
            elif byte == 0x20:                    # :168-187 MASTER_ACTIVATION
                waveform = bool(self.update_ctrl2 & DISP_START)
                if waveform:
                    self.visible = bytearray(self.ram)
                    self.frames += 1
                    self.nonwhite_visible = sum(1 for b in self.visible if b != 0xff)
                self.set_busy(True)
                self._pending = self.now + (self.waveform_ms if waveform
                                            else self.power_ms)
            return
        self.data_bytes += 1                      # :190-216 data path
        need = expected_params(self.command)
        if need and self.param_len < need:
            self.params[self.param_len] = byte
            self.param_len += 1
            if self.param_len == need:
                if self.command == 0x46 and byte == 0xF7:
                    self.ram = bytearray(b"\xff" * SIZE)
                    self.nonwhite_ram = 0
                else:
                    self.apply_params()
        elif self.command == 0x24:
            if self.x < WIDTH and self.y < HEIGHT:
                offset = self.y * ROW_BYTES + self.x // 8
                if self.ram[offset] != 0xFF:
                    self.nonwhite_ram -= 1
                if byte != 0xFF:
                    self.nonwhite_ram += 1
                    self.nonwhite_ram_writes += 1
                self.ram[offset] = byte
                self.ram_writes += 1
            self.advance_ram()

    def cmd(self, *bytes_):
        for b in bytes_:
            self.transfer(b, dc=False)

    def data(self, *bytes_):
        for b in bytes_:
            self.transfer(b, dc=True)

    # --- epd_advance_ram:129-148 ---------------------------------------------
    def advance_ram(self):
        inc_x = self.data_entry & 1
        inc_y = self.data_entry & 2
        line_end = (self.x + 8 > self.x_end) if inc_x else (self.x < self.x_end + 8)
        if line_end:
            self.x = self.x_start
            if inc_y:
                self.y = self.y + 1 if self.y < self.y_end else self.y_start
            else:
                self.y = self.y - 1 if self.y > self.y_end else self.y_start
        else:
            self.x = self.x + 8 if inc_x else self.x - 8

    # --- epd_apply_params:71-115 ---------------------------------------------
    def apply_params(self):
        p = self.params
        if self.command == 0x11 and self.param_len >= 1:
            self.data_entry = p[0]
        elif self.command == 0x44 and self.param_len >= 4:
            self.x_start = min((p[1] << 8) | p[0], WIDTH - 1)
            self.x_end = min((p[3] << 8) | p[2], WIDTH - 1)
        elif self.command == 0x45 and self.param_len >= 4:
            self.y_start = min((p[1] << 8) | p[0], HEIGHT - 1)
            self.y_end = min((p[3] << 8) | p[2], HEIGHT - 1)
        elif self.command == 0x4e and self.param_len >= 2:
            self.x = min((p[1] << 8) | p[0], WIDTH - 1)
        elif self.command == 0x4f and self.param_len >= 2:
            self.y = min((p[1] << 8) | p[0], HEIGHT - 1)
        elif self.command == 0x22 and self.param_len >= 1:
            self.update_ctrl2 = p[0]

    # --- epd_reset_gpio:55-65 --------------------------------------------------
    def reset_gpio(self, level):
        high = bool(level)
        if not high and self.rst_high:
            self._pending = None
            self.set_busy(False)
            self.clear_ram()
        self.rst_high = high

    def bus_reset(self):
        """EpdBus::reset() :221-229 — HIGH 10 / LOW 10 / HIGH 10, then 10 ms."""
        self.advance(10)
        self.reset_gpio(False)
        self.advance(10)
        self.reset_gpio(True)
        self.advance(10)

    def activation(self, ctrl2):
        self.cmd(0x22)
        self.data(ctrl2)
        self.cmd(0x20)

    def paint(self, n=4):
        """One RAM byte burst + waveform activation — a minimal displayed frame."""
        self.cmd(0x22)
        self.data(0xF7)
        self.cmd(0x24)
        self.data(*([0x00] * n))
        self.cmd(0x20)

    def enter_deep_sleep(self):
        """Ssd1677Driver.cpp:956-966 — powerOffController then 0x10 + mode 0x03."""
        self.cmd(0x22)
        self.data(0x03)
        self.cmd(0x20)
        self.advance(self.power_ms)               # firmware delay(200)+waitBusy
        self.cmd(0x10)
        self.data(0x03)

    # --- epd_reset:231-249 (accepted, patch 0051 ordering) ---------------------
    def device_reset(self):
        self.clear_ram()
        self.visible = bytearray(b"\xff" * SIZE)
        self.frames = self.commands = self.data_bytes = self.ram_writes = 0
        self.ignored_bytes = self.last_command = self.nonwhite_visible = 0
        self.nonwhite_ram_writes = self.cmd24 = self.cmd26 = 0
        self.update_ctrl2 = 0
        self.command = 0
        self.dc_high = True
        self.rst_high = True
        self._pending = None
        self.set_busy(False)
        self.busy_rise_edges = self.busy_fall_edges = self.busy_high_ms = 0
        self.busy_started_ms = 0


class PatchedPanel(Panel):
    """Mirror of experiments/0061-ssd1677-waveform-sleep.patch.

    Adds: busy_reason per armed window (NONE/POWER/WAVEFORM/RESET) with
    cumulative busy_power/waveform/reset_high_ms; deep-sleep state entered by
    command 0x10 that rejects every later transfer byte until a hardware RESET
    falling edge; post-reset BUSY window behind busy-on-reset-ms (default 0 =
    byte-for-byte accepted behaviour).  With the window enabled, RES release
    and SW_RESET arm it (last-writer attribution, no extra edges while BUSY
    is already high), because Ssd1677Driver.cpp:385-388 documents that BUSY
    may be asserted through the post-SWRESET settle.
    """

    NONE, POWER, WAVEFORM, RESET = 0, 1, 2, 3

    def __init__(self, waveform_ms=SHIPPED_WINDOW_MS, power_ms=SHIPPED_WINDOW_MS,
                 reset_ms=0):
        super().__init__(waveform_ms, power_ms)
        self.reset_ms = reset_ms
        self.deep_sleep = 0
        self.sleep_entries = 0
        self.sleep_ignored_bytes = 0
        self.busy_reason = self.NONE
        self.busy_power_high_ms = 0
        self.busy_waveform_high_ms = 0
        self.busy_reset_high_ms = 0

    def set_busy(self, busy):
        was_high = self.busy_level != 0
        now = self.now
        if busy != was_high:
            if busy:
                self.busy_rise_edges += 1
                self.busy_started_ms = now
            elif now > self.busy_started_ms:
                self.busy_fall_edges += 1
                high = now - self.busy_started_ms
                self.busy_high_ms += high
                if self.busy_reason == self.POWER:
                    self.busy_power_high_ms += high
                elif self.busy_reason == self.WAVEFORM:
                    self.busy_waveform_high_ms += high
                elif self.busy_reason == self.RESET:
                    self.busy_reset_high_ms += high
        self.busy_level = int(busy)
        if not busy:
            self.busy_reason = self.NONE

    def transfer(self, byte, dc):
        self.dc_high = bool(dc)
        if not self.rst_high:
            self.ignored_bytes += 1
            return
        if self.deep_sleep:                       # patch: sleep gate
            self.sleep_ignored_bytes += 1
            return
        if not self.dc_high:
            self.command = byte
            self.last_command = byte
            self.commands += 1
            if byte == 0x24:
                self.cmd24 += 1
            if byte == 0x26:
                self.cmd26 += 1
            self.param_len = 0
            if byte == 0x12:                      # SW_RESET
                self.clear_ram()
                self._pending = None
                if self.reset_ms:                 # patch: re-arm, do not release
                    self.busy_reason = self.RESET
                    self.set_busy(True)
                    self._pending = self.now + self.reset_ms
                else:
                    self.set_busy(False)
            elif byte == 0x10:                    # patch: ENTER DEEP-SLEEP
                self.deep_sleep = 1
                self.sleep_entries += 1
            elif byte == 0x20:
                waveform = bool(self.update_ctrl2 & DISP_START)
                if waveform:
                    self.visible = bytearray(self.ram)
                    self.frames += 1
                    self.nonwhite_visible = sum(1 for b in self.visible if b != 0xff)
                self.busy_reason = self.WAVEFORM if waveform else self.POWER
                self.set_busy(True)
                self._pending = self.now + (self.waveform_ms if waveform
                                            else self.power_ms)
            return
        self.data_bytes += 1
        need = expected_params(self.command)
        if need and self.param_len < need:
            self.params[self.param_len] = byte
            self.param_len += 1
            if self.param_len == need:
                if self.command == 0x46 and byte == 0xF7:
                    self.ram = bytearray(b"\xff" * SIZE)
                    self.nonwhite_ram = 0
                else:
                    self.apply_params()
        elif self.command == 0x24:
            if self.x < WIDTH and self.y < HEIGHT:
                offset = self.y * ROW_BYTES + self.x // 8
                if self.ram[offset] != 0xFF:
                    self.nonwhite_ram -= 1
                if byte != 0xFF:
                    self.nonwhite_ram += 1
                    self.nonwhite_ram_writes += 1
                self.ram[offset] = byte
                self.ram_writes += 1
            self.advance_ram()

    def reset_gpio(self, level):
        high = bool(level)
        if not high and self.rst_high:
            self._pending = None
            self.set_busy(False)
            self.clear_ram()
            self.deep_sleep = 0                   # patch: only HW reset wakes
        elif high and not self.rst_high and self.reset_ms:   # patch: trailing
            self._pending = None
            self.busy_reason = self.RESET
            self.set_busy(True)
            self._pending = self.now + self.reset_ms
        self.rst_high = high

    def enter_deep_sleep(self):
        self.cmd(0x22)
        self.data(0x03)
        self.cmd(0x20)
        self.advance(self.power_ms)
        self.cmd(0x10)
        self.data(0x03)                           # rejected: chip is asleep

    def device_reset(self):
        super().device_reset()
        self.deep_sleep = 0
        self.sleep_entries = self.sleep_ignored_bytes = 0
        self.busy_power_high_ms = self.busy_waveform_high_ms = 0
        self.busy_reset_high_ms = 0
        self.busy_reason = self.NONE


class GapAPowerVsWaveform(unittest.TestCase):
    """(a) power-on and pixel waveform share one 350 ms window by default,
    and the pooled busy-high-ms cannot attribute anything to either."""

    def test_accepted_defaults_measure_power_cost_as_350_by_construction(self):
        p = Panel()
        self.assertEqual(p.power_ms, p.waveform_ms)   # ybx_ssd1677.c:295-296
        p.activation(0xC0)                            # power-only, CTRL2 bit2=0
        p.advance(349)
        self.assertEqual(p.busy_level, 1)
        p.advance(1)
        self.assertEqual(p.busy_level, 0)
        power_ms = p.busy_high_ms
        p.paint()                                     # a real waveform frame
        p.advance(350)
        self.assertEqual(p.busy_high_ms - power_ms, 350,
                         "waveform cost is indistinguishable from power cost")

    def test_accepted_pooled_counter_has_no_attribution_fields(self):
        p = Panel(power_ms=40, waveform_ms=350)
        p.activation(0xC0)
        p.advance(40)
        p.paint()
        p.advance(350)
        self.assertEqual(p.busy_high_ms, 390)
        for field in ("busy_power_high_ms", "busy_waveform_high_ms"):
            self.assertFalse(hasattr(p, field),
                             f"{field} would need the 0061 patch to exist")

    def test_patched_model_separates_power_waveform_and_reset_windows(self):
        p = PatchedPanel(power_ms=40, waveform_ms=350, reset_ms=10)
        p.activation(0xC0)
        p.advance(40)
        self.assertEqual(p.busy_power_high_ms, 40)
        self.assertEqual(p.busy_waveform_high_ms, 0)
        p.paint()
        p.advance(350)
        self.assertEqual(p.busy_waveform_high_ms, 350)
        self.assertEqual(p.busy_power_high_ms, 40,
                         "tuning the waveform window no longer moves power cost")
        p.bus_reset()
        self.assertEqual(p.busy_reset_high_ms, 10)
        self.assertEqual(p.busy_power_high_ms + p.busy_waveform_high_ms
                         + p.busy_reset_high_ms, p.busy_high_ms)


class GapBDeepSleep(unittest.TestCase):
    """(b) 0x10 has no model state, so post-sleep painting still succeeds."""

    def test_accepted_model_paints_right_after_deep_sleep(self):
        p = Panel()
        p.paint()
        p.advance(350)
        frames = p.frames
        p.enter_deep_sleep()                              # 0x22 0x03, 0x20, 0x10, 0x03
        p.paint()
        p.advance(350)
        self.assertEqual(p.frames, frames + 1,
                         "gap: the controller would be deaf; QEMU is not")
        self.assertEqual(p.last_command, 0x20)
        self.assertFalse(hasattr(p, "deep_sleep"))
        self.assertEqual(p.busy_rise_edges, 3, "sleep produced no state at all")

    def test_patched_sleep_rejects_every_byte_until_hardware_reset(self):
        p = PatchedPanel(power_ms=40)
        p.paint()
        p.advance(400)
        frames = p.frames
        p.enter_deep_sleep()
        rises = p.busy_rise_edges                   # power-off edged once
        self.assertEqual(p.deep_sleep, 1)
        self.assertEqual(p.sleep_entries, 1)
        self.assertEqual(p.sleep_ignored_bytes, 1, "the mode byte 0x03 itself")
        p.paint(n=4)
        self.assertEqual(p.frames, frames, "post-sleep paint must not display")
        self.assertEqual(p.busy_rise_edges, rises, "post-sleep 0x20 must not busy")
        self.assertEqual(p.sleep_ignored_bytes, 1 + 8, "0x22 F7 0x24 x4 0x20")
        p.cmd(0x12)                                 # SW_RESET must not wake
        self.assertEqual(p.deep_sleep, 1)
        p.reset_gpio(False)                         # hardware RESET wakes
        self.assertEqual(p.deep_sleep, 0)
        p.reset_gpio(True)
        self.assertEqual(p.ram_writes, 4, "writes are from the pre-sleep paint")
        p.paint()
        p.advance(400)
        self.assertEqual(p.frames, frames + 1)

    def test_patched_sleep_keeps_the_glass_image_and_the_pending_release(self):
        p = PatchedPanel(power_ms=40)
        p.paint()                                   # visible = nonwhite frame
        p.advance(400)
        visible_before = bytes(p.visible)
        p.cmd(0x22)
        p.data(0x03)
        p.cmd(0x20)                                 # power-off arms 40 ms window
        self.assertEqual(p.busy_level, 1)
        p.cmd(0x10)
        p.data(0x03)
        p.advance(40)                               # window still completes
        self.assertEqual(p.busy_level, 0)
        self.assertEqual(bytes(p.visible), visible_before,
                         "deep sleep must not repaint the panel")


class GapCBusyAroundReset(unittest.TestCase):
    """(c) real panels trail BUSY around a hardware reset; the accepted model
    drives BUSY low on the falling edge and is silent on the rising edge."""

    def test_accepted_model_gives_no_signal_for_a_skipped_settle(self):
        p = Panel()
        p.reset_gpio(False)
        p.reset_gpio(True)                          # EpdBus::reset rising edge
        self.assertEqual(p.busy_level, 0,
                         "gap: no trailing BUSY to catch a missing delay(10)")
        self.assertEqual(p.busy_rise_edges, 0)
        p.cmd(0x12)
        p.advance(0)
        self.assertEqual(p.busy_level, 0,
                         "waitBusy after SWRESET returns instantly at t=0")

    def test_patched_reset_window_is_opt_in(self):
        p = PatchedPanel()                          # busy_on_reset_ms default 0
        p.reset_gpio(False)
        p.reset_gpio(True)
        self.assertEqual(p.busy_level, 0)
        p.advance(1000)
        self.assertEqual((p.busy_rise_edges, p.busy_fall_edges), (0, 0),
                         "default build behaves byte-for-byte as accepted")

    def test_patched_reset_trails_busy_and_a_skipped_settle_is_caught(self):
        p = PatchedPanel(reset_ms=10)
        p.reset_gpio(False)
        p.reset_gpio(True)                          # RES released
        self.assertEqual(p.busy_level, 1,
                         "panel still busy right after RES rises")
        p.cmd(0x12)                                 # initController: SWRESET
        self.assertEqual(p.busy_level, 1,
                         "SW_RESET re-arms instead of faking completion")
        p.advance(9)                                # the fixed delay(10)
        self.assertEqual(p.busy_level, 1,
                         "waitBusy after a skipped settle now blocks")
        p.advance(1)
        self.assertEqual(p.busy_level, 0)
        self.assertEqual(p.busy_reset_high_ms, 10)
        self.assertEqual(p.busy_rise_edges, p.busy_fall_edges)

    def test_patched_reset_falling_edge_cancels_an_armed_waveform_window(self):
        p = PatchedPanel(reset_ms=10)
        p.paint()                                   # arms 350 ms waveform
        p.advance(100)
        p.reset_gpio(False)                         # abort mid-window
        self.assertEqual(p.busy_level, 0)
        self.assertEqual(p.busy_waveform_high_ms, 100, "partial attribution")
        p.reset_gpio(True)
        self.assertEqual(p.busy_level, 1)           # reset window takes over
        p.advance(50)
        self.assertEqual(p.busy_level, 0)
        self.assertEqual(p.busy_reset_high_ms, 10)
        self.assertEqual(p._pending, None)


class ProbeContract(unittest.TestCase):
    """The orchestrator probe and this mirror must drive the same bytes."""

    def test_probe_sequences_match_the_driver_order(self):
        from probe_x4pro_epd_waveform_qtest import (byte_count, deep_sleep_sequence,
                                                    paint_sequence)
        flat = [b for _, buf in deep_sleep_sequence() for b in buf]
        self.assertEqual(flat, [0x22, 0x03, 0x20, 0x10, 0x03],
                         "Ssd1677Driver.cpp:618-622 then :965-966")
        flat = [b for _, buf in paint_sequence(4) for b in buf]
        self.assertEqual(flat[:1] + flat[2:5], [0x22, 0x24, 0x00, 0x00])
        self.assertEqual(byte_count(paint_sequence(4)), 8)

    def test_mirror_sleep_counters_match_probe_expectations(self):
        from probe_x4pro_epd_waveform_qtest import byte_count, paint_sequence
        p = PatchedPanel()
        p.cmd(0x10)
        p.data(0x03)
        self.assertEqual(p.sleep_ignored_bytes, 1)
        p.paint(n=4)
        self.assertEqual(p.sleep_ignored_bytes,
                         1 + byte_count(paint_sequence(4)))


if __name__ == "__main__":
    unittest.main()
