# 0037-agent-epd-busy — EPD BUSY wake semaphore, GPIO6 wiring, and the minimal QEMU fix

Date: 2026-10-30. Scope: read-only analysis of pinned SDK/firmware + isolated QEMU tree, one host-only model test. No firmware, no default-QEMU change.

## 1. The real driver code (FreeInk SDK 44427f8d, pinned by this repo)

The worktree submodule is not initialized; the analysis used an identical revision `44427f8d51866848b26bea01197f499f9fee5c4b` checked out at
`fw-native-account-review-20261029/freeink-sdk` (verified with `git submodule status`).

- X4 Pro profile: `libs/hardware/BoardConfig/include/BoardConfig.h:1835-1849` — `DisplayController::SSD1677`, 800x480, pins `{SCLK12, MOSI11, CS13, DC18, RST14, BUSY6}`. **BUSY is GPIO6, `BusyPolarity::ActiveHigh`** (`libs/display/FreeInkDisplay/src/bus/EpdBus.h:21`).
- Wake mechanism: `EpdBus.cpp:74` `static SemaphoreHandle_t s_epdRefreshDone`, created **once** in `EpdBus::begin()` (`EpdBus.cpp:158`, `xSemaphoreCreateBinary()`), called from `FreeInkDisplay.cpp:270` during `display.begin()`.
- `epdBusyIsr()` (`EpdBus.cpp:76-81`) only does `xSemaphoreGiveFromISR(s_epdRefreshDone)`.
- `EpdBus::waitRefreshComplete()` (`EpdBus.cpp:388-468`) for SSD1677: drain `take(0)` (:437) → `attachInterrupt(busy, epdBusyIsr, CHANGE)` (:438) → if BUSY already HIGH: loop `xSemaphoreTake(s_epdRefreshDone, pdMS_TO_TICKS(30000))` (:459-461); if BUSY LOW: single `xSemaphoreTake(..., pdMS_TO_TICKS(20))` (:445), on timeout detach and **return** (self-heal).
- Call sites in the render path: `Ssd1677Driver.cpp:526/559/597/643/895` (after `CMD_MASTER_ACTIVATION` 0x20) and `displayFinish()` (:640). No `setBusyWaitSliceHook`/`setBusyWaitHooks` is installed anywhere in `src/` or `lib/` of this firmware, so the ISR/semaphore path is the one that runs.

## 2. Does queue 0x3fcec294 plausibly equal the EPD BUSY wake semaphore? Yes.

From the linked factory ELF (`fw-092c2be5-symbols-20261029/.pio/build/x4pro/firmware.elf`, `nm`/`objdump`):

- `freeink::s_epdRefreshDone` static handle cell = **0x3fca1da8**; `epdBusyIsr` = 0x40375604 (IRAM); `EpdBus::waitRefreshComplete` = 0x4201e968. Disassembly loads 0x3fca1da8 at 0x4201e9c0 and calls `xQueueSemaphoreTake` (0x4038473c) at 0x4201e9e2 (drain), 0x4201ea12 (`movi a11,20` → 20 ticks), 0x4201ea6b (`0x7530` = 30000 ticks; `CONFIG_FREERTOS_HZ=1000` in the prebuilt SDK `…esp32s3/dio_opi/include/sdkconfig.h:1015`), with `__attachInterrupt(pin, epdBusyIsr, mode 3=CHANGE)` at 0x4201e9fe.
- Observed object 0x3fcec294: `ucQueueType=3` (`queueQUEUE_TYPE_BINARY_SEMAPHORE`), `uxLength=1`, `uxItemSize=0`, `uxMessagesWaiting=0` — exactly `xSemaphoreCreateBinary()`.
- Address arithmetic matches the IDF **SMP kernel** `Queue_t` (`framework-espidf/components/freertos/FreeRTOS-Kernel-SMP/queue.c:107-143` — no `pcTail`): `pcHead+0x00, pcWriteTo+0x04, u+0x08, xTasksWaitingToSend+0x0c, xTasksWaitingToReceive+0x24` (List_t = 24 B). The TCB event container 0x3fcec2b8 = base+0x24 ⇒ the render task sits on **xQueueSemaphoreReceive's blocked list with a finite timeout** (xDelayedTaskList1) — i.e. in one of the two EPD takes above.
- Ordering check: `renderingMutex` (0x3fcbc460) is created in the global `ActivityManager` ctor (`src/main.cpp:68` → `ActivityManager.cpp:40`, before `setup()`); EPD semaphore during `display.begin()` (`main.cpp:545`); render TCB (0x3fcec420) at `activityManager.begin()` (`main.cpp:547`, `xTaskCreatePinnedToCore` `ActivityManager.cpp:62`). Heap upward-growth 0x3fcbc460 < 0x3fcec294 < 0x3fcec420 fits.
- The only other binary semaphores in this guest are transient (`UsbSerialJtagHandoff.cpp:67-80`, deleted before reboot) or SDIO-only (`sdmmc_host.c:532`). No counting/recursive candidate matches type 3.

**One-word confirmation for the next GDB session:** `x/1wx 0x3fca1da8` — if it prints `0x3fcec294`, the identification is exact.

## 3. What the executed QEMU actually missed (and the important caveat)

The probe binary (qemu sha256 `27179636…`, per `s3-x4pro-tcb-0037b.json`) predates the busy wiring: the JSON shows `Property 'esp32s3.gpio.ybx-gpio-irq-level' not found`, panel telemetry `busy: 0`, and `GPIO_IN(status-lo)=0`. In that tree `hw/xtensa/esp32s3.c` aliases `ybx-epd-busy` for QMP but the device's `ybx.epd.busy` output line was **not connected** to `esp32s3.gpio "ybx.gpio-input" line 6`, and the GPIO device had no IRQ output into the interrupt matrix.

Therefore inside the guest: `digitalRead(6)==LOW` always ⇒ `waitRefreshComplete` takes the `:445` **20 ms fallback and returns** — it cannot block for multiple seconds. A host state-machine model of exactly this (see `0043-agent-epd-busy-model-test.c`, PASS) reproduces: shipped model self-heals at 20 ticks with zero IRQs. Consequence: the multi-second "render blocked on 0x3fcec294" snapshots from 0037b/0039 are consistent with the GDB-halted window catching the normal 20 ms wait (CCOUNT deltas between samples are tiny — CPUs were stopped), **not** with a proven 30 s hang. Busy stuck HIGH is impossible pre-0042 (pullups are bits 0|3|7 only, `esp32s3.c:774-777`). The EPD gap is real (no wake, fixed 20 ms tax per wait, and any future BUSY-HIGH modeling deadlocks each wait for 30 s), but the *touch-to-navigation* gate must be re-measured **without GDB** before blaming the EPD wait alone.

## 4. Minimal safe fix — status and review

`tools/ybx-system-emu/experiments/0042-s3-epd-busy-gpio-pcpu-irq.patch` (already applied to the working state of the read-only `qemu-rominit-20261029` tree, gated on the opt-in `ybx_x4pro_epd` property) is the correct minimal shape, and my review found its S3 register decoding faithful:

- `ybx.epd.busy` → GPIO input line 6 and `gpio.irq` → `ETS_GPIO_INTR_SOURCE` (both only when the X4 Pro EPD profile is enabled — GT911/touch behavior unchanged).
- Edge latching into `STATUS` only when the pin's `int_ena` (bit 13) is set, so the **pre-attach rising edge of 0x20 is ignored** — this is precisely what prevents the 0020-0022 re-run: no status bit exists before `attachInterrupt`, and `detachInterrupt` removes the ena bit, so a stale pulse cannot re-assert the line.
- `GPIO_PCPU_INT` (0x05c/0x068) exposed as the ISR status source and `STATUS_W1TC` (0x04c/0x058) ack honored — matching IDF S3 `gpio_ll_get_intr_status()`/`gpio_ll_clear_intr_status()` (`hal/esp32s3/include/hal/gpio_ll.h:114-143`, `soc/gpio_struct.h:136-150`: `int_type` bits 7-9, 5-bit `int_ena` from bit 13).
- Host model check (`0043-agent-epd-busy-model-test.c`): 0042 wakes the render task at the real falling edge (~351 ms virtual) with exactly one acked edge and the line deasserted; the same wiring **without** PCPU_INT exposure reproduces the stuck-line storm (>5000 re-deliveries, zero acks) — confirming the documented 0020-0022 root cause is the missing status/ack registers, not the busy pulses themselves.

Residual risks (non-blocking, for the human): a second concurrent GPIO-enabled input (only GT911 INT pin 10 exists; it self-clears by I2C read) re-asserts via the shared line — acceptable; per-CPU `int_ena` bits 13-17 are treated as one enable — benign for the single-ISR guest.

## 5. Recommended single next change

Rebuild the isolated QEMU from the current `qemu-rominit-20261029` tree (0042 applied) and re-run the 0039 More-tap probe **without GDB**, requiring, in order: `busy-level` 0→1→0 around the tap refresh, `ybx-gpio6-edges ≥ 1` and `ybx-gpio-irq-acks ≥ 1`, `frames` 2→3, `visible-hex` SHA change; plus the one-word guest check `*(0x3fca1da8) == 0x3fcec294`. If frames advance, EPD wait is no longer a gate; if they don't, the next suspect is elsewhere (GDB-perturbation hypothesis is then retired and real-time TCB sampling with QMP-only counters is needed).

## Evidence / files

- SDK: `EpdBus.cpp:74-81,148-158,388-468`; `EpdBus.h:21,160-193`; `Ssd1677Driver.cpp:526-643,895`; `BoardConfig.h:1835-1849`; `FreeInkDisplay.cpp:270`.
- Firmware: `src/main.cpp:68,531-547`; `src/activities/ActivityManager.cpp:40,62,80,514-528`; `lib/hal/HalDisplay.cpp:78-93`.
- ELF: `s_epdRefreshDone`=0x3fca1da8; `waitRefreshComplete`=0x4201e968; `epdBusyIsr`=0x40375604; disasm `xQueueSemaphoreTake` 20/30000-tick calls.
- Kernel: `FreeRTOS-Kernel-SMP/queue.c:107-143`; `sdkconfig.h:1015,1030`; S3 `gpio_ll.h:114-143`, `gpio_struct.h:136-150`.
- Probes: `qa-20261029/s3-x4pro-tcb-0037b.json`, `s3-x4pro-deadlock-0039.json` (busy:0, property missing, tick comparators ~1 kHz, CCOUNT frozen between samples).
- QEMU: `hw/display/ybx_ssd1677.c:148-167,233-237`; `hw/xtensa/esp32s3.c:774-777,949-990`; 0042 patch.
- Modified in this worktree: `tools/ybx-system-emu/experiments/0037-agent-epd-busy.md` (this file), `tools/ybx-system-emu/experiments/0043-agent-epd-busy-model-test.c` (host-only, PASS output above).
