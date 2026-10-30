# 0048-batch2-epd-lifecycle — SSD1677 BUSY ISR, panel lifecycle/sleep, GPIO6 false edges, and a review of patch 0037

Date: 2026-10-30. Lane: epd-lifecycle (batch 2, six-Qwen orchestration).

Scope actually executed: read-only analysis of the compiled QEMU tree, the pinned FreeInk
SDK and the factory firmware; one new **host-only** model test.
Not done, by instruction: no QEMU rebuild, no guest QEMU launch, no Docker, no firmware
change, no touch of the shared emulator/browser server, no patch shipped against
`qemu-rominit-20261029`. Every claim below is either a `file:line` reference or a result of
`tools/ybx-system-emu/test_x4pro_epd_lifecycle.py`.

Read-only sources used (paths as of this session):
- `~/.local/state/ybx-emulator-upstream/qemu-rominit-20261029` — patched checkout + compiled binary.
- `~/.local/state/xteink-agent-workspaces/fw-092c2be5-symbols-20261029/freeink-sdk` — SDK at the pinned revision.
- this worktree — CrossPoint firmware `src/` + `lib/hal/`.

## 1. What the compiled tree actually contains

`ybx.ssd1677` is the shipped panel model; patch **0042** (`0042-s3-epd-busy-gpio-pcpu-irq.patch`)
is applied to `hw/gpio/esp32_gpio.c` + `hw/xtensa/esp32s3.c`, patch **0037**
(`0037-x4pro-epd-busy-window-edge-telemetry.patch`) is **not**. Evidence: `hw/display/ybx_ssd1677.c`
has no `busy_rise_edges` field and no `epd_properties[]`; `esp32_gpio.c` does have
`GPIO_PCPU_INT`/`PCPU_INT1` at `:46-47`, `PIN_TYPE`/`PIN_ENA` at `:50-51`,
`esp32_gpio_update_irq` at `:70-90`, and `esp32s3.c` wires the panel BUSY line into the GPIO
input at `:985-987` (`"ybx.epd.busy"` → `"ybx.gpio-input"` line 6) with the GPIO IRQ into
`ETS_GPIO_INTR_SOURCE` at `:956`, all inside the `if (ms->ybx_x4pro_epd)` guards
(`esp32s3.c:824-848`, `:770-774`).

So 0037 can only be judged by reading plus host modelling — which is exactly what this lane did.

Shipped panel behaviour that matters here:
- `epd_set_busy:15-19` — `s->busy_level = busy; qemu_set_irq(s->busy, busy);`. It **already
  drives unconditionally** and keeps no edge history.
- `epd_refresh_completed:20-23` — the timer callback releases BUSY.
- `epd_reset_gpio:39-49` — only the **falling** RESET edge acts: `timer_del` + release BUSY +
  `epd_clear_ram`. The rising branch is empty.
- `epd_transfer:136-139` — every byte is ignored (and counted in `ignored_bytes`) while RST is low.
- `:147-151` — SW_RESET 0x12: clear RAM, `timer_del`, release BUSY.
- `:152-167` — MASTER_ACTIVATION 0x20: copy RAM→visible only if CTRL2 bit2 (`0x04`), then
  assert BUSY and arm **one hard-coded 350 ms window** (`:165-166`) for power-on and waveform alike.
- `epd_reset:211-225` — device reset: counters, `update_ctrl2 = 0`, `dc_high/rst_high = true`,
  `timer_del`, `epd_set_busy(s, false)`.

## 2. BUSY wake path, end to end (guest side is the production path)

X4 Pro: `BusyPolarity::ActiveHigh`, BUSY = GPIO6, and **no** `setBusyWaitSliceHook` /
`setBusyWaitHooks` is installed anywhere in this firmware, so the ISR/semaphore branch runs:

1. `Ssd1677Driver.cpp:376-390` `begin()` = `bus.reset()` + `initController()`; `initController`
   sends SWRESET, then the comment at `:385-387` states the contract: *"Active-high BUSY may not
   have asserted by the first GPIO sample, so `waitBusy()` alone is not a substitute for this
   delay"* → `delay(10)` then `waitBusy(" CMD_SOFT_RESET")`.
2. `EpdBus.cpp:202-230` `reset()` = RST HIGH 10 ms / LOW 10 ms / HIGH 10 ms (+ optional settle).
3. Render path: `EpdBus::waitRefreshComplete()` `:426-464`
   - `:426-429` if `s_epdRefreshDone` is null → plain polling `waitBusy()`.
   - `:437` `xSemaphoreTake(…, 0)` — **drain any stale token**.
   - `:438` `attachInterrupt(GPIO6, epdBusyIsr, CHANGE)`.
   - `:440` one level sample. If BUSY is already at the done level: `:445-448` take with a
     **20 ms** grace, on timeout `detachInterrupt` + return; else `:449-453` re-read the level
     and, if it is the done level, detach + return.
   - `:459-461` `while (digitalRead != doneLevel) { if (take(30000) != pdTRUE) break; }`, `:464` detach.
   - `epdBusyIsr` `:66-82` is only `xSemaphoreGiveFromISR(s_epdRefreshDone)`.

GPIO6 false-edge susceptibility, as actually configured:

- **No pullup on GPIO6.** `esp32s3.c:774-778` sets `board-input-pullups = (1U<<0)|(1U<<3)|(1U<<7)`
  (left key, power latch, right key). `esp32_gpio_reset_hold:208-220` restores exactly that, so
  after reset GPIO6 reads low. 0037's new comment (patch line 41) — *"GPIO6 may be parked high by
  a board pullup while the model still believes it is low"* — is **not true for this board** and
  should be corrected before merge; the "always drive" it justifies is already the shipped
  behaviour (`epd_set_busy:17-18`).
- **A rising edge that precedes `attachInterrupt` is not latched.** `esp32_gpio_inject_input:92-119`
  stores `status |= mask` only `if (edge && PIN_ENA(cfg))` (`:113`); `attachInterrupt` is what
  writes `int_ena` (via `PIN_x_CFG`, `esp32_gpio_write:192-197`). This is what avoids the
  0020-0022 re-run, and `test_normal_refresh_busy_rises_before_attach_and_fall_wakes` proves it
  (`edges6 == 0` at 0x20, one wake at the falling edge).
- **STATUS is a bitmask, so two edges before the ISR dispatch coalesce into one interrupt.**
  `esp32_gpio.c:114` (`|=`) plus `update_irq:88` (`(*status & flag) && (PIN_ENA(cfg) & 1)`).
  Proven in `test_stale_token_from_aborted_refresh_is_drained`: `edges6 == 2`, `status == 1<<6`,
  one ISR run, one ack.
- **`detachInterrupt` clears `int_ena` but never STATUS** (`esp32_gpio_write:192-196` only writes
  CFG). A re-arm can therefore assert the GPIO line immediately. The SDK absorbs it: the drain at
  `:437` removes the stale token, and the level re-read at `:449-453` stops the wait from
  reporting a frame that never completed. Modelled by
  `test_stale_status_rearm_is_absorbed_by_the_driver_level_recheck` — cost is one wasted ISR wake,
  never a false "refresh done".

## 3. Panel reboot / suspend-resume / sleep workflows

Firmware side (this worktree):
- Sleep: `src/main.cpp:474-529` `enterDeepSleep()` → `display.deepSleep()` (`:524`) **before**
  `powerManager.startDeepSleep()` (`:528`); `lib/hal/HalDisplay.cpp:102` → `einkDisplay.deepSleep()`
  → `Ssd1677Driver.cpp:956-966` = `powerOffController()` (`:614-626`: border 0x80, CTRL2 0x03,
  0x20, `delay(200)`, `waitBusy`) then `0x10` + data `0x03`.
  `HalPowerManager.cpp:84-140` documents that `powerDownRailsForSleep()` must run after
  `display.deepSleep()` and is a **no-op on X4/X3** — i.e. on X4 Pro the EPD rail stays powered
  through deep sleep; only the controller is asleep.
- Wake is a chip reset (`src/main.cpp:531-549` `setupDisplayAndFonts()` → `display.begin(seamless)`
  at `:545`, `lib/hal/HalDisplay.cpp:15-36`; call sites `src/main.cpp:646` for silent reboot and
  `:765` for boot resume). Non-seamless wake additionally arms `requestResync()` on
  PowerButton/AfterFlash/Other, so the first paint after sleep is an absolute clean — consistent
  with the SDK comment that deep-sleep mode 2 discards controller RAM.
- In QEMU, a wake therefore exercises: `EpdBus::reset()` (RESET pulse) → `epd_reset_gpio:43-47`
  (cancel any armed window, release BUSY, clear RAM) → SWRESET → `delay(10)` → `waitBusy`.
  `test_commands_ignored_while_reset_low_and_sw_reset_cancels_window` and
  `test_reset_pulse_leaves_no_phantom_window` cover exactly that sequence.

Findings that need no code change but do need to be recorded:
- **Retired premise.** The earlier hypothesis "an aborted refresh emits a late completion IRQ"
  is refuted by the shipped code: the RESET falling edge already runs `timer_del` before
  `epd_set_busy(false)` (`ybx_ssd1677.c:43-48`), and any residue is drained at `EpdBus.cpp:437`.
  For that reason this lane ships **no** QEMU patch — the fix would model a bug that cannot occur.
- **`system_reset` is quiet for GPIO6 in both device-reset orders.** Either order (EPD reset first
  → BUSY driven low into a still-armed CHANGE interrupt → one latched bit, then
  `esp32_gpio_reset_hold` clears STATUS/CFG; or GPIO first → `pin_cfg == 0`, so nothing latches)
  ends with `irq_level == 0`, `edges6 == 0` and the input line low. Proven by
  `test_epd_and_gpio_reset_ordering_does_not_fabricate_an_irq`; it also proves the 0037 defect in §4.
- **Fidelity gap (documented, not invented around).** Real SSD1677 panels *trail* BUSY around a
  hardware reset — that is precisely why `Ssd1677Driver.cpp:385-387` demands a fixed 10 ms settle.
  The QEMU panel does the opposite: `epd_reset_gpio:43-47` and `epd_reset:224` **drive BUSY low**.
  So QEMU cannot catch firmware that skips the post-reset settle. Recorded as
  `test_shipped_model_asserts_no_busy_on_reset`; no synthetic "busy-on-reset" knob was added,
  because there is no repository evidence for a real duration and fabricating one would be worse
  than the gap.
- **Fidelity gap: no deep-sleep state at all.** There is no `0x10` branch in `epd_transfer:147-167`,
  and `epd_expected_params:100-112` gives `0x10` zero parameters, so the mode byte of
  `Ssd1677Driver.cpp:966` is dropped at `:171`. A post-sleep `0x24`/`0x20` still writes RAM, still
  counts a frame and still raises BUSY. Consequence for the whole batch: **QEMU must not be cited
  as evidence for sleep/resume regressions** (paint-after-sleep, missing wake reset). Recorded as
  `test_deep_sleep_has_no_model_state_so_post_sleep_paint_still_works`.
- **Timing fidelity.** `:165-166` uses one 350 ms constant for power-on and for a pixel waveform,
  so any per-refresh "power cost" measured through QEMU is 350 ms by construction
  (`test_shipped_window_is_one_constant_for_power_and_waveform`).
- Together these mean the multi-second "render blocked on the EPD semaphore" snapshots of lanes
  0037b/0039 remain unproven in this tree: with BUSY low, `waitRefreshComplete` takes the 20 ms
  branch at `:445` and returns. Any future BUSY-HIGH modelling would instead park each wait for
  the full 30 s (`:459-461`), which is exactly why the wake wiring has to be right before more
  windows are lengthened.

## 4. Review of `0037-x4pro-epd-busy-window-edge-telemetry.patch`

Verdict: **accept the shape, require two edits before it is used as measurement evidence.**
It is telemetry only for the default configuration (`busy-waveform-ms`/`busy-power-ms` both
default to 350), the new properties are read-only, and the three soc aliases are added inside the
existing `if (ss->ybx_x4pro_epd)` block, so GT911/touch and non-X4 boards are untouched. The edge
math itself is correct and reproduces the guest contract (`test_0037_telemetry_edge_math`).

1. **Defect (must fix) — counter clear ordering, hunk `@@ -217,6 +237,8 @@`.** The patch inserts
   `busy_rise_edges = busy_fall_edges = busy_high_ms = 0; busy_started_ms = 0;` immediately after
   `s->update_ctrl2 = 0;`, i.e. **before** `timer_del` + `epd_set_busy(s, false)` at the end of
   `epd_reset`. A `system_reset` taken while BUSY is high then reports the release of the panel's
   own window as a *fall edge that was never risen*, and because `busy_started_ms` was zeroed
   first, `busy_high_ms` accumulates `now - 0` — the whole VM uptime. Reproduced exactly by
   `test_0037_epd_reset_clears_counters_before_releasing_busy`: as-authored `(rise, fall) = (0, 1)`
   and `busy_high_ms == panel.now`; cleared-last `(0, 0, 0, 0)`. Fix: move those two lines after
   `epd_set_busy(s, false)` (one-hunk move, no behaviour change for any non-telemetry field).
2. **Comment (must fix)** at patch line 41: GPIO6 is not pulled up on X4 Pro (§2), and
   `epd_set_busy:17-18` already always drives. Leave the `qemu_set_irq` call as it is; correct the
   rationale text so the next reader does not go hunting for a pullup that does not exist.
3. **Optional, useful:** with the two properties settable, a lane can model power-on separately
   from a waveform, but 350/350 is the only configuration matching today's guest evidence. Do not
   ship non-default values without a real-panel measurement.

## 5. Tests

New file: `tools/ybx-system-emu/test_x4pro_epd_lifecycle.py` — pure `unittest`, no QEMU, no
Docker, no new dependency, 14 tests:

```
$ python3 tools/ybx-system-emu/test_x4pro_epd_lifecycle.py
Ran 14 tests in 0.001s  OK
$ python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py'
Ran 177 tests in 0.419s  OK
```

Discovery safety was checked: the sibling `test_x4pro_epd_busy_semantics.py` defines no
`unittest.TestCase` (its `QTest`/`Epd` helpers and `main()` sit behind `--qemu`), so the
`discover` run never launches the shared emulator. Total suite time stays under half a second.

Coverage: boot idle (no stale IRQ); normal refresh (pre-attach rise ignored, fall wakes);
RESET-low byte rejection + SW_RESET window cancel; reset pulse leaves no phantom window; stale
token drained + STATUS coalescing; GPIO6 false-edge susceptibility for all five interrupt types;
stale STATUS across re-arm absorbed by `EpdBus.cpp:449-453`; power-off and deep sleep
deterministic; post-sleep paint still accepted (gap); single 350 ms constant; 0037 edge math;
0037 reset ordering (defect + fix); EPD/GPIO reset ordering; BUSY state asserted on reset (gap).

## 6. Risks and limits of this evidence

- The test is a **Python mirror** of `ybx_ssd1677.c` + patched `esp32_gpio.c` + `EpdBus.cpp`, line
  by line, not the devices themselves. It proves logic ordering and register semantics; it cannot
  prove wall-clock behaviour, QEMU main-loop scheduling, or the real panel. A merged 0037 must
  still be re-measured against the compiled binary by whichever lane rebuilds.
- Only GPIO6 is tracked in the model. The real device shares one STATUS register with GT911 INT
  (pin 10), which self-clears on I2C read; that interaction is out of scope here and remains the
  residual risk already recorded in `0037-agent-epd-busy.md` §4.
- No claim is made about Wi-Fi (not emulated) or about touch-to-navigation latency. Per §3, the
  EPD wait cannot block for seconds in this tree, so the navigation gate must be re-measured
  **without GDB** before being attributed to the EPD.

## 7. Recommended single next change (for the human)

Apply the two 0037 edits in §4 (move the counter clear after `epd_set_busy(s, false)`; correct the
pullup comment), keep defaults 350/350, and only then let a rebuild lane use
`busy-rise-edges`/`busy-fall-edges`/`busy-high-ms` as measurement evidence.
