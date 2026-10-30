# 0037 — cross-core IPI lane: was the render stall a lost QEMU IPI?

Answer: **no.** The recorded post-tap state is fully explained by FreeRTOS semantics plus a
completion semaphore that QEMU never lets anyone give. The QEMU ESP32-S3 cross-core path is
implemented end to end and its interrupt-matrix geometry matches the real chip.

## 1. QEMU models the whole IPI chain

| Stage | File:line | Behaviour |
| --- | --- | --- |
| `SYSTEM_CPU_INTR_FROM_CPU_0..3` (0x600C0030/34/38/3C) | `hw/xtensa/esp32s3_clk.c:32-42` | bit set → latch `s->levels` + `qemu_set_irq(…, 1)`; clear → lower |
| Reads | `hw/xtensa/esp32s3_clk.c:26-29` | return latched level, no side effects (safe for probes) |
| Clock → matrix | `hw/xtensa/esp32s3.c:898-901` | `irqs[i]` → `ETS_FROM_CPU_INTR0_SOURCE + i` (79..82) |
| Source → per-CPU interrupt | `hw/xtensa/esp32s3_intc.c:29-45, 46-58` | `irq_map[cpu][source]`, then `extint[]` lookup |
| Matrix → CPU | `hw/xtensa/pic_cpu.c:35-59` | `INTSET & (INTENABLE | NMI)` per level → `CPU_INTERRUPT_HARD` |

Geometry cross-checked (this closed the layout risk): IDF `DR_REG_INTERRUPT_BASE = 0x600C2000`
(`components/soc/esp32s3/register/soc/reg_base.h:61`) equals QEMU
`include/hw/misc/esp32s3_reg.h:70`; core0 map entry for source 79 is `+0x13C` and core1 is
`+0x93C` (`interrupt_core0_reg.h:649`, `interrupt_core1_reg.h:17,649`), i.e. one word per source
with a 0x800 stride per CPU — exactly QEMU's `get_map_entry()` (`addr/4`, `ESP32S3_INT_MATRIX_INPUTS
= 0x800/4`). Word-per-source is correct; no patch needed here.

One latent divergence found: QEMU resets **every** map entry to 6 (`esp32s3_intc.c:20,
INTMATRIX_UNINT_VALUE`) while the real default for the FROM_CPU maps is 16
(`interrupt_core0_reg.h:650`). Neither 6 nor 7 is a dc232b external interrupt
(`target/xtensa/core-dc232b/core-isa.h:299-315`, `XCHAL_NUM_EXTINTERRUPTS = 17`), so an
unprogrammed source is dropped silently, without any `qemu_log_mask` warning. It is not the cause
of this stall because IDF routes explicitly: `esp_crosscore_int_init()` allocates
`SYS_CPU_INTR_FROM_CPU_0_SOURCE` on core0 and `…_1_…` on core1
(`esp_system/crosscore_int.c:102-104`) through `esp_rom_route_intr_matrix`
(`esp_hw_support/intr_alloc.c:663, 903`); `esp_crosscore_int_send()` marks the reason then pokes the
other core (`:112-120`), and the receiving ISR clears the bit before doing any work (`:42-49`).

## 2. `pdPASS` never meant "the task was woken"

`xTaskGenericNotify` (IDF 5.5.5 `FreeRTOS-Kernel/tasks.c`):

- `:5905` starts with `xReturn = pdPASS` and `eIncrement` (`:5929`) cannot change it;
- `:5921` writes `ucNotifyState = taskNOTIFICATION_RECEIVED` (2) **unconditionally**;
- `:5971` only unblocks the task when the state it *found* was `taskWAITING_NOTIFICATION` (1);
- inside that branch: `:5974` add-to-ready, `:5995-5999` `taskIS_YIELD_REQUIRED` →
  `taskYIELD_IF_USING_PREEMPTION`; the remote half is `prvIsYieldRequiredSMP` issuing
  `taskYIELD_CORE(!xCurCoreID)` at `:852-858` → `vPortYieldOtherCore` → IPI.

Observed in 0036/0039: `ucNotifyState 0→2`, `ulNotifiedValue 0→1`, and the TCB still linked into
`xDelayedTaskList1` with its event item inside `Queue_t` 0x3fcec294 (`ucQueueType=3`,
`uxLength=1`, `uxItemSize=0`, `uxMessagesWaiting=0`). The 0→2 transition is the *latch*, not the
wake: the gate at `:5971` was false, so **no IPI was ever requested** and the emulator had
nothing to lose. Nothing on the ready list existed for the target core's tick
(`freertos/port_systick.c:190-241`) to dispatch, which is why the extra thousands of systimer
matches recorded during the stall changed nothing.

## 3. The test that separates the two hypotheses

`tools/ybx-system-emu/experiments/0043-x4pro-ipi-vs-semaphore.gdb` — read-only: breakpoints plus
memory/MMIO reads only, no register or memory writes, no injected interrupt.

- Requests: `0x40384ffc vPortYieldOtherCore`, `0x4037675c esp_crosscore_int_send_yield`,
  `0x40376710 esp_crosscore_int_send`; armed only between the notify call (`0x4210225d`) and its
  return (`0x42102260`), so a token there can only come from the notify path.
- Delivery: `0x403766b4 esp_crosscore_isr` (the target CPU actually took the IPI).
- Other hypothesis: `0x403845e0 xQueueGiveFromISR` (was the semaphore ever given?).
- State dump after the notify: priority, `xCoreID`, `ucNotifyState`, `ulNotifiedValue`, and list
  membership by comparing `xStateListItem.pvContainer` / `xEventListItem.pvContainer` against
  `&xDelayedTaskList1`, `&pxReadyTasksLists[prio]`, `&xPendingReadyList[0..1]`, the semaphore
  fields, plus `0x600C0030..3C` (latched FROM_CPU levels) and `0x600C213C..140` /
  `0x600C293C..940` (per-core FROM_CPU routing).
- Expected result for the recorded stall: **zero `IPI_REQ_*` tokens**, state container
  `0x3fcac00c`, event container inside the Queue_t, `SEM_MESSAGES=0`, `SEM_QUEUE_TYPE=3`, no
  `SEMAPHORE_GIVE_FROM_ISR` → verdict `unrelated_semaphore_wait`.

Names and addresses were verified against the factory ELF
(`fw-092c2be5-symbols-20261029/.pio/build/x4pro/firmware.elf`): `nm` gives the four function
addresses above; `&xDelayedTaskList1 = 0x3fcac00c`, `&xPendingReadyList[1] = 0x3fcabfdc`,
`&pxReadyTasksLists[4] = 0x3fcac070`; DWARF in this build names the container member
**`pvContainer`** (not `pxContainer`) and `ucQueueType`/`xTasksWaitingToReceive` do exist.

`tools/ybx-system-emu/crosscore_ipi_verdict.py` turns that log (optionally plus the QEMU counters)
into one verdict: `ipi_requested_but_not_recorded_by_emulator`,
`ipi_reached_matrix_but_unrouted_on_target_cpu`, `ipi_delivered_but_cpu_kept_idle`,
`unrelated_semaphore_wait`, `ready_but_not_dispatched_despite_ticks`, `insufficient_evidence`.
Host tests: `tools/ybx-system-emu/test_crosscore_ipi_verdict.py` (12 cases; whole suite 120 OK).

## 4. Instrumentation, kept opt-in

`experiments/0043-s3-optin-crosscore-ipi-counters.patch` adds counters and read-only QOM
properties on `/machine/soc/clock` and `/machine/soc/intmatrix`: FROM_CPU set/clear counts and the
latched level, matrix programming count with last address/value, delivered vs unrouted asserts per
CPU, and FROM_CPU-only assert/route/default-route counters. It follows the already-used
`object_property_add_uint32_ptr(..., OBJ_PROP_FLAG_READ)` pattern from
`0024-s3-systimer-readonly-comparator-counters.patch` and never injects an interrupt.
Verified with `patch -p1 --dry-run` and `git apply --check` against `qemu-rominit-20261029`;
**not compiled, not run** (no rebuild allowed on the shared tree). It must stay out of
`apply_qemu_patches.sh` until a human builds and boots it.

## 5. Root cause ownership

The missing hardware event is the panel BUSY completion: QEMU never ran `freeink::epdBusyIsr`
(`0x40375604`), so `freeink::s_epdRefreshDone` (0x3fca1da8) was never given and the render task
stayed blocked inside `currentActivity->render()`. That finding, the GPIO `GPIO_PCPU_INT_REG` fix
`0042-s3-epd-busy-gpio-pcpu-irq.patch`, and the control run 0040 that finally produced a real
SSD1677 RAM change (`e79c2ad5… → 8b81cec4…`, non-white 4743 → 7274, no GDB) belong to the
`fw-qemu-evidence-20261029` evidence lane. This lane did not re-run a guest and claims no
navigation success.

## 6. Not proven here

- The 0043 patch has never been compiled or booted; property paths and counter values are
  unverified at runtime.
- Per-CPU `INTSET`/`INTENABLE` were not observed (one Xtensa CPU per GDB connection); use the
  monitor for that.
- Which core ran `ActivityManager::loop` versus where the render task is pinned comes from the
  0036/0039 logs, not from a fresh measurement.
- The reset-default divergence (6 vs 16) is a static finding; only the counters can show whether
  the guest ever depends on it.

## 7. Single recommended next change

Build the 0043 patch into a **separate** QEMU checkout and run one non-GDB boot + More tap with
`0042` in place, then read the eight FROM_CPU counters: expected `ybx-cpu-intr-0-sets > 0`,
`ybx-cpu<0|1>-from-cpu-asserted > 0`, `ybx-cpu<0|1>-from-cpu-default-route == 0`,
`ybx-map-writes > 0`. Any non-zero `unrouted`/`default-route` there is the real emulator bug to
fix (map the FROM_CPU sources at reset to a valid extint, as silicon does with 16); zero counters
across the board with a working UI confirm this lane's conclusion — the IPI path was never asked
to wake anything.
