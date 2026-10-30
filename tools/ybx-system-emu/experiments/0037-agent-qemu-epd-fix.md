# 0037 — QEMU EPD lane: X4 Pro SSD1677 BUSY semantics vs the GPIO6 semaphore

Date: 2026-10-30. Scope: opt-in QEMU SSD1677 model (patch lineage 0015→0018→0042) and
X4 Pro BUSY/IRQ semantics only. No production firmware change, no default machine change,
no push, no merge. The patched QEMU tree was used read-only.

## Verdict

The 0036 symptom ("notification returns pdPASS but the render task never renders") is the
**FreeInk BUSY completion wait timing out inside `waitRefreshComplete()`**, not a lost
cross-core interrupt, not a lock-ordering bug, and not an Xtensa ABI problem. The panel
model and the GPIO6 wiring are semantically correct once GPIO interrupts reach the guest
(0042). Remaining model gaps are fidelity/cost, not the navigation gate.

## Guest-side BUSY contract (exact evidence)

Factory firmware == this source tree compiled: the symbol-bearing ELF
`fw-092c2be5-symbols-20261029/.pio/build/x4pro/firmware.elf` matches `src/` +
`freeink-sdk/` 1:1, so the driver source is authoritative (no guessing).

| Fact | Reference |
| --- | --- |
| X4 Pro pins `{sclk,mosi,cs,dc,rst,busy}` = `{12,11,13,18,14,6}` | `freeink-sdk/libs/hardware/BoardConfig/include/BoardConfig.h:1849` |
| BUSY is **active-high**, idle LOW | `FreeInkDisplay/src/driver/Ssd1677Driver.h:86` |
| One shared binary semaphore for completion | `FreeInkDisplay/src/bus/EpdBus.cpp:158` (`xSemaphoreCreateBinary`) |
| ISR gives it on the pin edge | `EpdBus.cpp:76-81` (`epdBusyIsr` + `portYIELD_FROM_ISR`) |
| ISR armed as **CHANGE** (both edges), only during a wait | `EpdBus.cpp:438` |
| Assert grace: `xSemaphoreTake(..., pdMS_TO_TICKS(20))` | `EpdBus.cpp:445` |
| Completion wait: `while (digitalRead(6) != LOW) xSemaphoreTake(..., pdMS_TO_TICKS(30000))` | `EpdBus.cpp:459-461` |
| Render task = `ActivityManagerRender`, core 1, takes RenderLock then renders | `src/activities/ActivityManager.cpp:63,77-91` |
| Refresh path | `Ssd1677Driver.cpp:523-526` → `HalDisplay.cpp:78` |

`uxLength=1, uxItemSize=0, ucQueueType=3 (queueSEMAPHORE)` observed at guest
`0x3fcec294` matches `s_epdRefreshDone`, and a **timed** take (the 30 000 ms value) is
exactly why the TCB sits on `xDelayedTaskList1`. So "blocked on another queue while
holding renderingMutex" is the designed behaviour of a refresh wait, not a deadlock.

## QEMU-side BUSY semantics (measured, not assumed)

New host test `tools/ybx-system-emu/test_x4pro_epd_busy_semantics.py` drives the real
GP-SPI2 MMIO path and the GPIO registers of the **already built 0042 binary**
(SHA256 `4106cff2795eaaaab79d0cf074aba0fa4acbc98e9ea520e93f5721f2aa07e7be`) with no flash
attached, so every result is device-model behaviour. 24/24 checks pass
(`0037-epd-busy-qtest-report.json`):

- `0x22`+`0x20` with CTRL2 `0xF7` → BUSY high, visible in `GPIO_IN 0x3C` bit 6 (this is
  what `digitalRead(6)` reads), then low after **350 virtual ms** (5 ms sampling).
- With `GPIO_PIN6` armed as CHANGE (`int_type=3`, `int_ena` bit set) **both** edges latch
  `GPIO_STATUS 0x44` and `GPIO_PCPU_INT 0x5C` and raise the machine GPIO line;
  `GPIO_STATUS_W1TC 0x4C` clears both per edge → two ISR events per refresh, which is
  exactly the assert+complete pair `EpdBus.cpp:438-461` expects.
- Edge while the pin is **disarmed** leaves no stale STATUS bit and no IRQ level —
  the property whose absence caused the 0020/0022 CPU1 stall.
- Level type (`int_type=5`) re-asserts after acknowledgement while BUSY is still high
  (hardware-true); a level-driven guest ISR would storm. The FreeInk CHANGE path avoids it.
- Frozen-clock probe (`-S`, `0037-epd-busy-qtest-report-frozen.json`): BUSY stays high
  through 1500 ms of `clock_step`, because the release timer is on `QEMU_CLOCK_VIRTUAL`.
  A debugger halt therefore **defers** the completion edge; it cannot be observed as a
  wake in a stopped guest. This is a measurement artifact, not a guest bug.
- `0x22=0xC0` (power-on, CTRL2 bit2 clear) does not paint a frame (correct) but holds
  BUSY for the same **350 ms**.

## Refuted hypotheses (do not re-open)

1. **`PIN_ENA` off-by-one in `esp32_gpio.c`.** Checked against
   `framework-espidf/components/soc/esp32s3/register/soc/gpio_struct.h:130-149`: the S3 pin
   field is `int_type[9:7]`, `wakeup_enable[10]`, `config[12:11]`, `int_ena[17:13]`, so
   `PIN_TYPE (>>7)&7` and `PIN_ENA (>>13)&0x1f` are **correct**. `GPIO_LL_INTR_ENA` is
   `BIT(0)` and `gpio_ll_intr_enable_on_core()` ignores `core_id`
   (`hal/esp32s3/include/hal/gpio_ll.h:33,165-170`), so `PIN_ENA(cfg) & 1` matches what
   IDF actually writes for both cores. The observed `PIN6 = 0x2180` decodes as
   `int_type=3 (CHANGE)` + `int_ena bit0` — consistent. No patch here.
2. **Cross-core IPI / notification loss.** `xTaskGenericNotify` → pdPASS + `ucNotifyState`
   0→2 was already correct in 0036; the notification is consumed as soon as the render
   task leaves the refresh wait.
3. **`uiReady` pointer corruption (0025)** — withdrawn in 0036; nothing in this lane
   contradicts that withdrawal.

## Remaining gaps worth fixing (model fidelity, not the gate)

- **A power/booster-only activation is modelled as long as a full waveform**
  (`ybx_ssd1677.c:164-166` in the current tree: one hard-coded 350 ms window for every
  `0x20`). FreeInk polls BUSY after those activations
  (`Ssd1677Driver.cpp:610 "gray power-on"`, `:624 " display power-down"`), so each refresh
  cycle burns up to 700 extra virtual ms inside the render task, and a QA report cannot
  tell a genuine stuck panel from a modelled power ramp.
- **No panel-side edge telemetry**: `busy-level` is a sample, so "released while the guest
  was halted", "never armed", and "never happened" are indistinguishable in a report.
- Known and accepted: `0x12` SWRESET clears BUSY immediately (real panel holds it a few
  ms), so `waitBusy(" CMD_SOFT_RESET")` (`Ssd1677Driver.cpp:389`) returns at once. Left as
  is; changing it touches the init sequence for no gate benefit.

## Patch: `0037-x4pro-epd-busy-window-edge-telemetry.patch`

Minimal, opt-in (`ybx-x4pro-epd=on` only), three files, ~50 lines:

- `epd_set_busy()` counts real transitions (`busy-rise-edges`, `busy-fall-edges`,
  accumulated `busy-high-ms`) but still drives the line unconditionally, because a board
  pull-up can park GPIO6 high while the model believes it is low.
- `0x20` selects `busy-waveform-ms` (CTRL2 bit2 set) or `busy-power-ms` (power-only).
  **Both default to 350**, so RAM, `visible`, `frames` and every existing SHA are unchanged
  unless the operator opts in.
- Machine aliases `ybx-epd-busy-{rise,fall}-edges`, `ybx-epd-busy-high-ms`; the windows are
  settable without another code change, e.g.
  `-global driver=ybx.ssd1677,property=busy-power-ms,value=5`.

Validation done here (no QEMU rebuild, tree untouched):
`git apply --check` succeeds against the rominit tree and `--reverse --check` fails (proof
it is not already applied there); both patched translation units pass the real build
command with `-Wall -Werror -fsyntax-only` inside `ybx-qemu-builder:20261029`.
Runtime behaviour of the non-default knob is **not** yet measured — it needs one rebuild.
The host test already asserts the new counters (4 rises = 4 falls after its four
activations) and degrades to "absent (pre-0037 build)" on today's binary.

## Recommended single next change

Keep the QEMU IRQ code as landed in 0042 and **rebuild once with 0037 applied**, then rerun
the two non-GDB gates:
`python3 tools/ybx-system-emu/test_x4pro_epd_busy_semantics.py --qemu … --rom …`
(new counters present, BUSY 350 ms for `0xF7`, `busy-power-ms=5` shrink for `0xC0`), and
`probe_x4pro_guest_touch_ui.py` with `-global …busy-power-ms=5`, requiring the Home frame
SHA `e79c2ad5f2b60b16fe3f20625742d5f1343f5785552d1b32c11f33c42a874672` to stay byte-identical
before the tap and the More frame SHA `8b81cec46b2dd5690cc2748d80d01a7ee86ca7ffbb2d57c7a92bcb68354222cf`
to appear with `frames` increased, in a run without GDB. Only then is panel-verified
navigation claimed. Physical e-paper timing is still unverified and remains human hardware
work.

## Files modified (this worktree only)

- `tools/ybx-system-emu/test_x4pro_epd_busy_semantics.py` (new host/QTest)
- `tools/ybx-system-emu/experiments/0037-x4pro-epd-busy-window-edge-telemetry.patch` (new)
- `tools/ybx-system-emu/experiments/0037-epd-busy-qtest-report.json`,
  `0037-epd-busy-qtest-report-frozen.json` (generated evidence)
- `tools/ybx-system-emu/experiments/0037-agent-qemu-epd-fix.md` (this note)
