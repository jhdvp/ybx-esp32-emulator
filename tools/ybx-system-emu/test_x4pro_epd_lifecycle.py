#!/usr/bin/env python3
"""Host-only lifecycle model for the X4 Pro SSD1677 BUSY ISR path (no QEMU run).

Mirrors, line-for-line, the three layers that must agree for a GPIO6 busy-edge
wake to be correct, so panel reboot/suspend races can be proven without
touching the shared emulator:

  panel:  hw/display/ybx_ssd1677.c of qemu-rominit-20261029 — epd_set_busy:15-19
          (always drives, no edge memory), epd_refresh_completed:20-23,
          epd_reset_gpio:39-49 (RESET falling = timer_del + release + clear RAM;
          rising branch empty), epd_transfer:136-139 (ignore while RST low),
          :147-151 (SW_RESET), :152-167 (0x20 arms one hard-coded 350 ms
          window for power-on *and* waveform), epd_reset:211-225;
  irq:    experiments/0042-s3-epd-busy-gpio-pcpu-irq.patch on
          hw/gpio/esp32_gpio.c — PIN_TYPE/PIN_ENA:50-51,
          esp32_gpio_update_irq:70-90, esp32_gpio_inject_input:92-119 (edge
          latch gated by PIN_ENA:113), STATUS/PCPU_INT read :137-140 and
          PIN_x_CFG read :142-144, esp32_gpio_write STATUS_W1TC ack :181-189
          + PIN_x_CFG store :192-197,
          esp32_gpio_reset_hold:208-220; hw/xtensa/esp32s3.c board-input-pullups
          = bits 0|3|7 :774-778 (GPIO6 is *not* pulled);
  guest:  freeink-sdk EpdBus.cpp — s_epdRefreshDone/epdBusyIsr :66-82,
          reset() :202-230 (HIGH 10 / LOW 10 / HIGH 10 ms), waitBusy()
          ActiveHigh :306-317 (1 ms poll, 30 s break), waitRefreshComplete()
          :426-464 (drain :437 -> attach(CHANGE) :438 -> level sample :440 ->
          20 ms grace :445-453 -> 30 s loop :459-461 -> detach :464),
          Ssd1677Driver.cpp begin/initController :376-390, powerOn :604-611,
          powerOffController :614-626, deepSleep :956-966,
          Ssd1677Driver.h:86 BusyPolarity::ActiveHigh.

Characterisations (tests, not fixes):
  * test_reset_pulse_leaves_no_phantom_window — the shipped RESET handler
    already cancels the in-flight refresh timer, so an aborted refresh cannot
    emit a stale completion IRQ later; the only residue is one *real* fall edge,
    and EpdBus.cpp:437 drains that token before the next wait.
  * test_epd_reset_clears_counters_after_releasing_busy — 0037 as authored
    cleared the counters before epd_set_busy(s, false), so a system_reset
    inside a BUSY-high window yielded fall_edges=1, rise_edges=0 and
    busy_high_ms == full VM uptime. Patch 0051 moves the clear after the
    release; this test pins the fixed ordering, and
    probe_x4pro_telemetry_qtest.py reproduces it against the compiled binary.
  * test_shipped_model_asserts_no_busy_on_reset — model fidelity gap: real
    SSD1677 panels assert BUSY around a hardware reset (Ssd1677Driver.cpp:385-388
    says Active-high BUSY may not have asserted by the first sample), the QEMU
    panel drives BUSY *low* on the RESET falling edge, so QEMU cannot catch
    firmware that skips the post-reset settle.
  * test_deep_sleep_has_no_model_state_so_post_sleep_paint_still_works — the
    model has no SSD1677 deep-sleep gate at all, so sleep/resume regressions are
    invisible here; QEMU must not be cited as evidence for them.
"""
from __future__ import annotations

import unittest

DISP_START = 0x04

# ESP32-S3 GPIO_PIN register layout (soc/gpio_struct.h via patch 0042).
INT_TYPE_SHIFT, INT_ENA_SHIFT = 7, 13
TYPE_POSEDGE, TYPE_NEGEDGE, TYPE_ANYEDGE = 1, 2, 3
TYPE_LOWLEVEL, TYPE_HIGHLEVEL = 4, 5

# esp32s3.c:774-778 — board-input-pullups = (1U<<0)|(1U<<3)|(1U<<7).
S3_BOARD_INPUT_PULLUPS = (1 << 0) | (1 << 3) | (1 << 7)
SHIPPED_WINDOW_MS = 350  # ybx_ssd1677.c:152-167, one constant for power+waveform


def cfg(int_type: int, int_ena: int = 0) -> int:
    return (int_type << INT_TYPE_SHIFT) | (int_ena << INT_ENA_SHIFT)


class Panel:
    """ybx_ssd1677.c state that matters for BUSY/lifecycle.

    telemetry=False mirrors the compiled model; telemetry=True adds the
    edge/duration counters of experiments/0037 (its `was_high` guard means the
    counters only move on a real level change, while qemu_set_irq still fires
    on every call, exactly like the shipped epd_set_busy:17-18).
    """

    def __init__(self, waveform_ms=SHIPPED_WINDOW_MS, power_ms=SHIPPED_WINDOW_MS,
                 telemetry=False):
        self.waveform_ms, self.power_ms = waveform_ms, power_ms
        self.telemetry = telemetry
        self.now = 0
        self.busy_level = 0
        self.rise_edges = self.fall_edges = self.busy_high_ms = 0
        self.busy_started_ms = 0
        self.ignored_bytes = 0
        self.frames = 0
        self.commands = 0
        self.update_ctrl2 = 0
        self.command = 0
        self.dc_high = True
        self.rst_high = True
        self._pending = None  # fire_ms, armed by the 0x20 window
        self.gpio = None

    # --- virtual clock -----------------------------------------------------
    def advance(self, ms):
        """Runs the armed refresh timer in fire order, like the QEMU timer list."""
        end = self.now + ms
        if self._pending is not None and self._pending <= end:
            self.now = self._pending
            self._pending = None
            self.set_busy(0)          # epd_refresh_completed:20-23
        self.now = end

    # --- hw/display/ybx_ssd1677.c ------------------------------------------
    def set_busy(self, level, arm_window=False):
        was_high = self.busy_level != 0
        if arm_window:
            self._pending = self.now + self._window_ms()
        if self.telemetry and bool(level) != was_high:
            if level:
                self.rise_edges += 1
                self.busy_started_ms = self.now
            elif self.now > self.busy_started_ms:
                self.fall_edges += 1
                self.busy_high_ms += self.now - self.busy_started_ms
            else:
                self.fall_edges += 1
        self.busy_level = level
        if self.gpio is not None:  # epd_set_busy always drives the line
            self.gpio.inject(6, level)

    def _window_ms(self):
        return self.waveform_ms if self.update_ctrl2 & DISP_START else self.power_ms

    def ctrl2(self, value):
        self.update_ctrl2 = value

    def cmd(self, byte):
        self.transfer(byte, dc=False)

    def transfer(self, byte, dc):
        if not self.rst_high:                 # :136-139 bytes ignored in reset
            self.ignored_bytes += 1
            return
        self.commands += 1
        if dc:
            return
        self.command = byte
        if byte == 0x12:            # SW_RESET: clear_ram + timer_del + release
            self._pending = None
            self.set_busy(0)
        elif byte == 0x20:          # MASTER_ACTIVATION
            if self.update_ctrl2 & DISP_START:
                self.frames += 1
            self.set_busy(1, arm_window=True)

    def _reset_gpio(self, level):
        """epd_reset_gpio:39-49 — only the falling branch does anything."""
        high = bool(level)
        if not high and self.rst_high:
            self._pending = None
            self.set_busy(0)
        self.rst_high = high

    def bus_reset(self, extra_settle_ms=0):
        """EpdBus::reset() :221-229 — HIGH 10, LOW 10, HIGH 10, optional settle."""
        self.advance(10)
        self._reset_gpio(0)
        self.advance(10)
        self._reset_gpio(1)
        self.advance(10)
        if extra_settle_ms:
            self.advance(extra_settle_ms)

    def _clear_counters(self):
        self.rise_edges = self.fall_edges = self.busy_high_ms = 0
        self.busy_started_ms = 0

    def device_reset(self):
        """epd_reset():211-225 with patch 0051 ordering.

        The telemetry counters are cleared after the pending window and BUSY are
        released; clearing first would count the reset-driven fall without its
        rise and add the whole VM uptime to busy_high_ms.
        """
        self.frames = self.commands = 0
        self.ignored_bytes = 0
        self.update_ctrl2 = 0
        self.command = 0
        self.dc_high = True
        self.rst_high = True
        self._pending = None
        self.set_busy(0)
        if self.telemetry:
            self._clear_counters()


class Gpio:
    """hw/gpio/esp32_gpio.c as patched by 0042 (single tracked pin, GPIO6)."""

    def __init__(self):
        self.input = bool(S3_BOARD_INPUT_PULLUPS >> 6 & 1)
        self.status = 0
        self.pin_cfg = 0
        self.irq_level = 0
        self.acks = 0
        self.edges6 = 0

    def arm(self, value):
        # esp32_gpio_write:192-196 PIN_x_CFG = v, then esp32_gpio_update_irq()
        self.pin_cfg = value
        self.update_irq()

    def inject(self, n, level):
        if n != 6:
            return
        old = self.input
        self.input = bool(level)
        high = bool(level)
        itype = (self.pin_cfg >> INT_TYPE_SHIFT) & 7
        ena = (self.pin_cfg >> INT_ENA_SHIFT) & 0x1F
        edge = ((itype == TYPE_POSEDGE and not old and high)
                or (itype == TYPE_NEGEDGE and old and not high)
                or (itype == TYPE_ANYEDGE and old != high))
        if edge and ena:                       # :113 latch only when enabled
            self.status |= 1 << 6
            self.edges6 += 1
        if edge or itype in (TYPE_LOWLEVEL, TYPE_HIGHLEVEL):
            self.update_irq()

    def update_irq(self):
        itype = (self.pin_cfg >> INT_TYPE_SHIFT) & 7
        ena = (self.pin_cfg >> INT_ENA_SHIFT) & 0x1F
        if itype in (TYPE_LOWLEVEL, TYPE_HIGHLEVEL):
            want = self.input if itype == TYPE_HIGHLEVEL else not self.input
            if want:
                if ena:
                    self.status |= 1 << 6
            else:
                self.status &= ~(1 << 6)
        self.irq_level = int(bool(self.status & (1 << 6)) and bool(ena & 1))

    def ack(self):
        # STATUS_W1TC :181-185
        self.status &= ~(1 << 6)
        self.acks += 1
        self.update_irq()

    def reset(self):
        """esp32_gpio_reset_hold:208-220."""
        self.input = bool(S3_BOARD_INPUT_PULLUPS >> 6 & 1)
        self.status = 0
        self.pin_cfg = 0
        self.irq_level = 0
        self.acks = 0
        self.edges6 = 0


class EpdDriver:
    """FreeInk EpdBus/Ssd1677Driver consumer of GPIO6 (factory contract).

    X4 Pro is ActiveHigh and not OnePage, and no host installs
    setBusyWaitSliceHook, so waitRefreshComplete() takes the ISR path
    (EpdBus.cpp:426-464) — this class models that path only.
    """

    def __init__(self, panel: Panel, gpio: Gpio):
        self.panel, self.gpio = panel, gpio
        panel.gpio = gpio
        self.token = 0            # s_epdRefreshDone binary semaphore
        self.attached = False
        self.isr_runs = 0
        self.premature_exits = 0  # waitRefreshComplete ended while BUSY high
        self.zero_token_drains = 0

    # --- EpdBus.cpp:66-82 --------------------------------------------------
    def epd_busy_isr(self):
        self.isr_runs += 1
        self.token = 1

    def service_irq(self):
        while self.gpio.irq_level:
            self.epd_busy_isr()
            self.gpio.ack()

    def read_busy(self):
        return self.gpio.input

    # --- boot / suspend ----------------------------------------------------
    def begin(self, extra_settle_ms=0):
        """Ssd1677Driver::begin :376-390."""
        self.panel.bus_reset(extra_settle_ms)
        self.panel.cmd(0x12)          # CMD_SOFT_RESET
        self.panel.advance(10)        # :388 fixed settle
        return self.wait_busy()

    def attach(self):
        self.attached = True
        self.gpio.arm(cfg(TYPE_ANYEDGE, int_ena=1))   # :438 CHANGE

    def detach(self):
        self.attached = False
        self.gpio.arm(0)

    def wait_busy(self, limit_ms=30000):
        """EpdBus.cpp:306-317 ActiveHigh: poll digitalRead(6) until LOW."""
        start = self.panel.now
        while self.read_busy():
            self.panel.advance(1)
            if self.panel.now - start > limit_ms:
                return False
        return True

    def wait_refresh_complete(self):
        """EpdBus.cpp:426-464, the semaphore path with no slice hook."""
        self.token = 0                                        # :437 take(0)
        self.zero_token_drains += 1
        self.attach()                                         # :438
        self.service_irq()                                    # pending GPIO irq
        saw_working = self.read_busy()                        # :440
        if not saw_working:                                   # :441-454
            self.panel.advance(20)                            # :445 take(20 ms)
            self.service_irq()
            if not self.token:
                return self._exit()                           # :446-447
            saw_working = self.read_busy()                    # :449
            if not saw_working:
                return self._exit()                           # :450-453
        deadline = self.panel.now + 30000                     # :459-461
        while self.read_busy():
            self.panel.advance(1)
            self.service_irq()
            self.token = 0
            if self.panel.now > deadline:
                self.detach()
                return False
        return self._exit()

    def _exit(self):
        self.detach()
        if self.read_busy():
            self.premature_exits += 1
        return not self.read_busy()

    # --- driver-level sequences -------------------------------------------
    def power_on(self):
        """Ssd1677Driver.cpp:604-611: CTRL2=0xC0, 0x20, waitBusy."""
        self.panel.ctrl2(0xC0)
        self.panel.cmd(0x20)
        return self.wait_busy()

    def power_off_controller(self):
        """Ssd1677Driver.cpp:614-626: border, CTRL2=0x03, 0x20, delay(200), waitBusy."""
        self.panel.ctrl2(0x03)
        self.panel.cmd(0x20)
        self.panel.advance(200)
        return self.wait_busy()

    def deep_sleep(self):
        """Ssd1677Driver.cpp:956-966: powerOffController, then 0x10 + data 0x03."""
        ok = self.power_off_controller()
        self.panel.cmd(0x10)               # unknown to the model: no-op
        self.panel.transfer(0x03, dc=True)
        return ok


class EpdLifecycleTests(unittest.TestCase):
    def new_env(self, **kw):
        panel, gpio = Panel(**kw), Gpio()
        return panel, gpio, EpdDriver(panel, gpio)

    def test_boot_idle_busy_low_no_stale_irq(self):
        panel, gpio, drv = self.new_env()
        self.assertTrue(drv.begin())
        self.assertEqual(panel.busy_level, 0)
        self.assertEqual(gpio.irq_level, 0)
        self.assertEqual(gpio.status, 0)
        self.assertEqual(gpio.edges6, 0)
        self.assertEqual(drv.isr_runs, 0)

    def test_normal_refresh_busy_rises_before_attach_and_fall_wakes(self):
        panel, gpio, drv = self.new_env()
        self.assertTrue(drv.begin())
        panel.ctrl2(0xF7)
        panel.cmd(0x20)                    # rising edge: int_ena still 0
        self.assertEqual(gpio.edges6, 0)   # 0042 :113 gate — no pre-attach latch
        self.assertTrue(drv.wait_refresh_complete())
        self.assertEqual(drv.premature_exits, 0)
        self.assertEqual(gpio.edges6, 1)   # only the falling edge wakes
        self.assertEqual(drv.isr_runs, 1)
        self.assertEqual(gpio.acks, 1)
        self.assertEqual(panel.frames, 1)
        self.assertEqual(panel.busy_level, 0)
        self.assertFalse(drv.attached)     # :464 always detaches

    def test_commands_ignored_while_reset_low_and_sw_reset_cancels_window(self):
        panel, gpio, drv = self.new_env()
        self.assertTrue(drv.begin())
        panel.ctrl2(0xF7)
        panel.cmd(0x20)
        self._reset_low_and_ignore(panel)
        self.assertEqual(panel.ignored_bytes, 1)
        panel.rst_high = True
        panel.cmd(0x12)                    # SW_RESET: timer_del + release
        panel.advance(400)
        self.assertEqual(panel.busy_level, 0)
        self.assertEqual(gpio.edges6, 0)   # level already low: no edge

    @staticmethod
    def _reset_low_and_ignore(panel):
        panel.rst_high = False             # EPD held in reset
        panel.cmd(0x20)

    def test_reset_pulse_leaves_no_phantom_window(self):
        """Aborted refresh cannot emit a late completion IRQ (shipped :43-47)."""
        panel, gpio, drv = self.new_env()
        self.assertTrue(drv.begin())
        panel.ctrl2(0xF7)
        panel.cmd(0x20)                    # 350 ms window armed
        panel.advance(5)
        panel.bus_reset()                  # resume/sleep RESET pulse aborts it
        self.assertIsNone(panel._pending, "epd_reset_gpio already timer_del()s")
        self.assertEqual(panel.busy_level, 0)
        self.assertTrue(drv.wait_refresh_complete())   # BUSY low -> :445 grace
        self.assertEqual(drv.isr_runs, 0)
        panel.advance(400)                 # the window's original deadline
        self.assertEqual(gpio.edges6, 0, "no stale timer may reach GPIO6")
        self.assertEqual(drv.isr_runs, 0)

    def test_stale_token_from_aborted_refresh_is_drained(self):
        """An armed ISR sees the abort as one wake; :437 eats its stale token.

        STATUS is a bitmask (esp32_gpio.c:114 `|=`), so the rising edge of the
        aborted refresh and the RESET-low release coalesce into a single
        interrupt: epdBusyIsr runs once and leaves one token behind.
        """
        panel, gpio, drv = self.new_env()
        self.assertTrue(drv.begin())
        drv.attach()                       # host armed the ISR before the abort
        panel.ctrl2(0xF7)
        panel.cmd(0x20)                    # rising edge, latched
        panel.advance(5)
        panel.bus_reset()                  # RESET-low releases BUSY: second edge
        self.assertEqual(gpio.edges6, 2)
        self.assertEqual(gpio.status, 1 << 6, "both edges share one STATUS bit")
        drv.service_irq()                  # one ISR wake, token left behind
        self.assertEqual((drv.isr_runs, drv.token, gpio.acks), (1, 1, 1))
        drv.detach()
        drains = drv.zero_token_drains
        self.assertTrue(drv.wait_refresh_complete())
        self.assertEqual(drv.zero_token_drains, drains + 1)
        self.assertEqual(gpio.edges6, 2, "no third edge from the aborted refresh")
        self.assertEqual(drv.isr_runs, 1, "no second wake from the aborted refresh")
        self.assertEqual(drv.premature_exits, 0)
        self.assertFalse(drv.attached)

    def test_gpio6_false_edge_susceptibility(self):
        panel, gpio, drv = self.new_env()
        self.assertTrue(drv.begin())
        drv.attach()
        # epd_set_busy drives unconditionally (:18); a same-level repeat cannot
        # fabricate a GPIO edge because inject_input compares to the stored level.
        before = gpio.edges6
        panel.set_busy(0)
        panel.set_busy(0)
        self.assertEqual(gpio.edges6, before)
        # BUSY raised while detached, then re-armed: 0042 latches edges only
        # while PIN_ENA is set, so no STATUS bit is inherited across attach.
        drv.detach()
        panel.set_busy(1)
        self.assertEqual(gpio.status, 0)
        gpio.arm(cfg(TYPE_ANYEDGE, int_ena=1))
        self.assertEqual(gpio.status, 0)
        self.assertEqual(gpio.irq_level, 0)
        # falling while detached is invisible too
        drv.detach()
        panel.set_busy(0)
        self.assertEqual(gpio.irq_level, 0)
        # level types are self-reasserting: FreeInk CHANGE (edge) semantics must
        # be emulated, a HIGHLEVEL config would storm epdBusyIsr
        panel.set_busy(1)
        gpio.arm(cfg(TYPE_HIGHLEVEL, int_ena=1))
        self.assertEqual(gpio.irq_level, 1)
        gpio.ack()
        self.assertEqual(gpio.irq_level, 1, "level type re-asserts while high")
        panel.set_busy(0)
        gpio.ack()
        self.assertEqual(gpio.irq_level, 0)
        gpio.arm(0)

    def test_stale_status_rearm_is_absorbed_by_the_driver_level_recheck(self):
        """detach() clears int_ena, not STATUS (:192-196) — a re-arm can assert
        immediately; EpdBus.cpp:449-453's level re-read stops it being a lie."""
        panel, gpio, drv = self.new_env()
        self.assertTrue(drv.begin())
        drv.attach()
        panel.set_busy(1)                  # completion edge ...
        panel.set_busy(0)
        self.assertEqual(gpio.status, 1 << 6)
        drv.detach()                       # ... left un-acked across the detach
        self.assertEqual(gpio.irq_level, 0)
        # re-arm while BUSY is LOW: stale STATUS + fresh int_ena asserts at once,
        # so epdBusyIsr runs without any real edge
        self.assertTrue(drv.wait_refresh_complete())
        self.assertEqual(drv.isr_runs, 1, "stale STATUS re-asserted on re-arm")
        self.assertEqual(gpio.edges6, 2, "both latched edges predate this wait")
        self.assertEqual(panel.frames, 0, "no frame was claimed")
        self.assertEqual(drv.premature_exits, 0)
        self.assertFalse(drv.attached)

    def test_power_off_and_deep_sleep_stay_deterministic(self):
        panel, gpio, drv = self.new_env()
        self.assertTrue(drv.begin())
        self.assertTrue(drv.power_on())              # CTRL2 0xC0, no frame
        self.assertEqual(panel.frames, 0)
        self.assertTrue(drv.power_off_controller())  # CTRL2 0x03 + 200 ms
        self.assertTrue(drv.deep_sleep())
        self.assertEqual(panel.busy_level, 0)
        self.assertEqual(gpio.irq_level, 0)
        self.assertEqual(drv.premature_exits, 0)

    def test_deep_sleep_has_no_model_state_so_post_sleep_paint_still_works(self):
        """Characterisation of a fidelity gap, not a fix.

        `ybx.ssd1677` never implements SSD1677 deep sleep: there is no 0x10
        branch in epd_transfer:147-167 and epd_expected_params:100-112 gives 0x10
        zero parameters, so the 0x03 mode byte of Ssd1677Driver.cpp:966 is
        dropped at :171. A real controller then ignores every SPI byte until a
        hardware RESET pulse (EpdBus.cpp:202-230 plus initController's SWRESET).
        Consequence for this lane: firmware that paints after deepSleep() — or
        that skips the wake reset — still renders and still raises BUSY here, so
        QEMU cannot be evidence about that class of sleep/resume bug. Only a
        real panel can.
        """
        panel, gpio, drv = self.new_env()
        self.assertTrue(drv.begin())
        self.assertTrue(drv.deep_sleep())
        self.assertEqual(panel.ignored_bytes, 0)  # nothing gates on sleep state

        panel.ctrl2(0xF7)                          # a post-sleep waveform...
        panel.cmd(0x20)                            # ...is still accepted
        self.assertEqual(panel.busy_level, 1)
        self.assertTrue(drv.wait_refresh_complete())
        self.assertEqual(panel.frames, 1)
        panel.device_reset()                       # epd_reset(), not a RESET pulse
        self.assertEqual(panel.frames, 0)

    def test_shipped_window_is_one_constant_for_power_and_waveform(self):
        """ybx_ssd1677.c:152-167 — power-on costs the same 350 ms as a waveform."""
        panel, gpio, drv = self.new_env()
        self.assertTrue(drv.begin())
        panel.ctrl2(0xC0)
        self.assertEqual(panel._window_ms(), SHIPPED_WINDOW_MS)
        panel.ctrl2(0xF7)
        self.assertEqual(panel._window_ms(), SHIPPED_WINDOW_MS)
        t0 = panel.now
        self.assertTrue(drv.power_on())
        self.assertEqual(panel.now - t0, SHIPPED_WINDOW_MS)
        # 0037's busy-power-ms would let a lane tell the two apart; the compiled
        # model cannot, so any per-refresh power cost measured here is 350 ms.

    def test_0037_telemetry_edge_math(self):
        panel, gpio, drv = self.new_env(waveform_ms=350, power_ms=50,
                                         telemetry=True)
        self.assertTrue(drv.begin())
        for _ in range(2):
            panel.ctrl2(0xF7)
            panel.cmd(0x20)
            panel.advance(351)
        panel.ctrl2(0xC0)
        panel.cmd(0x20)
        panel.advance(51)
        self.assertEqual((panel.rise_edges, panel.fall_edges), (3, 3))
        self.assertEqual(panel.busy_high_ms, 350 + 350 + 50)
        # same-level set_busy must not double-count (0037 was_high guard)
        panel.set_busy(1)
        panel.set_busy(1)
        panel.advance(10)
        panel.set_busy(0)
        self.assertEqual((panel.rise_edges, panel.fall_edges), (4, 4))
        self.assertEqual(panel.busy_high_ms, 750 + 10)

    def test_epd_reset_clears_counters_after_releasing_busy(self):
        """Patch 0051: a reset inside a BUSY-high window leaves no phantom fall."""
        panel, gpio, drv = self.new_env(telemetry=True)
        self.assertTrue(drv.begin())
        panel.advance(500)                 # keep the VM clock away from zero
        panel.ctrl2(0xF7)
        panel.cmd(0x20)                    # rise at now, busy_started_ms = now
        panel.advance(120)
        self.assertEqual((panel.rise_edges, panel.fall_edges), (1, 0))
        panel.device_reset()               # e.g. system_reset from /api/reboot
        self.assertEqual((panel.rise_edges, panel.fall_edges,
                          panel.busy_high_ms, panel.busy_started_ms), (0, 0, 0, 0))
        self.assertEqual(panel.busy_level, 0)
        self.assertEqual(gpio.irq_level, 0)

    def test_idle_reset_also_zeroes_telemetry(self):
        panel, gpio, drv = self.new_env(telemetry=True)
        self.assertTrue(drv.begin())
        panel.ctrl2(0xF7)
        panel.cmd(0x20)
        panel.advance(351)                 # normal completion
        self.assertEqual((panel.rise_edges, panel.fall_edges), (1, 1))
        panel.device_reset()
        self.assertEqual((panel.rise_edges, panel.fall_edges,
                          panel.busy_high_ms, panel.busy_started_ms), (0, 0, 0, 0))

    def test_epd_and_gpio_reset_ordering_does_not_fabricate_an_irq(self):
        """system_reset hits both devices; either order must end with GPIO6 quiet."""
        for epd_first in (True, False):
            with self.subTest(epd_first=epd_first):
                panel, gpio, drv = self.new_env(telemetry=True)
                self.assertTrue(drv.begin())
                drv.attach()               # guest ISR armed, int_ena = 1
                panel.ctrl2(0xF7)
                panel.cmd(0x20)
                panel.advance(120)         # mid-waveform, BUSY high
                if epd_first:
                    panel.device_reset()
                    gpio.reset()
                else:
                    gpio.reset()
                    panel.device_reset()
                self.assertEqual(gpio.status, 0)
                self.assertEqual(gpio.irq_level, 0)
                self.assertEqual(gpio.edges6, 0)
                self.assertEqual(gpio.pin_cfg, 0)
                self.assertEqual(panel.rst_high, True)
                # GPIO6 is not in board-input-pullups (esp32s3.c:774-778), so the
                # post-reset line is released low, matching a quiescent panel.
                self.assertFalse(gpio.input)
                self.assertTrue(drv.wait_refresh_complete())
                self.assertEqual(drv.isr_runs, 0)
                self.assertEqual(drv.premature_exits, 0)

    def test_shipped_model_asserts_no_busy_on_reset(self):
        """Fidelity gap: a real panel trails BUSY around RESET; QEMU drives it low."""
        panel, gpio, drv = self.new_env()
        self.assertTrue(drv.begin())
        panel.ctrl2(0xF7)
        panel.cmd(0x20)
        panel.advance(5)
        panel._reset_gpio(0)
        self.assertEqual(panel.busy_level, 0, "epd_reset_gpio:45 releases BUSY")
        panel._reset_gpio(1)               # :48 rising branch is empty
        self.assertEqual(panel.busy_level, 0)
        self.assertEqual(panel.ignored_bytes, 0)
        panel.cmd(0x12)                    # commands accepted with no settle
        self.assertEqual(panel.ignored_bytes, 0)
        self.assertEqual(panel.rise_edges, 0)
        # Ssd1677Driver.cpp:385-388 says ActiveHigh BUSY may not have asserted by
        # the first sample on real silicon, so a firmware regression that drops
        # the post-reset settle is invisible to this model — it must not be
        # "verified" here.


if __name__ == "__main__":
    unittest.main(verbosity=2)
