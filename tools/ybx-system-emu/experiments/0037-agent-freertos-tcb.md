# 0037-agent — reverse-mapping Queue_t 0x3fcec294 and the render TCB 0x3fcec420

Date: 2026-10-30. Agent lane: FreeRTOS TCB/queue forensics (read-only, this worktree only).
Status: **mapping solved and confirmed against the shipped build; the wake gap is FreeRTOS
semantics, not a QEMU IPI failure.** No firmware change proposed.

All offsets/symbols below were extracted from the matching factory ELF
`fw-092c2be5-symbols-20261029/.pio/build/x4pro/firmware.elf` (DWARF + `readelf` +
`objdump`) and from the local kernel source tree
`~/.platformio/packages/framework-espidf/components/freertos/FreeRTOS-Kernel/`
(IDF 5.5.5). DWARF line tables of the shipped objects select exactly these files:
`.../esp-idf/components/freertos/FreeRTOS-Kernel/tasks.c|queue.c`; `info line
*0x40387444` → tasks.c:5903, `*0x4038473c` → queue.c:1699, `*0x4201ea6e` →
`freeink-sdk/libs/display/FreeInkDisplay/src/bus/EpdBus.cpp:460` (SDK rev 44427f8).
The host test `test_0037_freertos_queue_mapping.py` (this directory, 4/4 passing)
re-asserts every constant quoted here.

## 1. The address mapping

DWARF offsets for `struct QueueDefinition` (shipped build):

| field | offset | value at 0x3fcec294 |
| --- | --- | --- |
| `pcHead` | +0x00 | (storage) |
| `u.xSemaphore.xMutexHolder` / `uxRecursiveCallCount` | +0x08 / +0x0C | unused for type 3 |
| `xTasksWaitingToSend` | +0x10 | — |
| **`xTasksWaitingToReceive`** | **+0x24** | **0x3fcec294+0x24 = 0x3fcec2b8** |
| `uxMessagesWaiting` | +0x38 | 0 |
| `uxLength` / `uxItemSize` | +0x3C / +0x40 | 1 / 0 |
| `ucQueueType` | +0x50 (inside `xQueueLock` tail, computed; verified via GDB field addr) | 3 |

So the recorded "event list pvContainer 0x3fcec2b8" is exactly
`&((Queue_t*)0x3fcec294)->xTasksWaitingToReceive` — the render task is queued in
**that semaphore's receive-wait list**. Nothing is corrupted; the two addresses
are the same object's base and its waiter list header.

TCB (`struct tskTaskControlBlock`, size 352): `pxTopOfStack` +0x00,
`xStateListItem` +0x04 (its `pvContainer` at +0x14, wake `xItemValue` at +0x04),
`xEventListItem` +0x18 (its `pvContainer` at +0x28), `uxPriority` +0x2C,
`pcTaskName` +0x34, `xCoreID` +0x44, `uxMutexesHeld` +0x58,
`ulNotifiedValue` +0x158, `ucNotifyState` +0x15C.
Observed 0x3fcec420 fields: state container 0x3fcac00c == `&xDelayedTaskList1`
(readelf: `xDelayedTaskList1 @0x3fcac00c`), event container 0x3fcec2b8 ==
`&queue(0x3fcec294).xTasksWaitingToReceive`.

## 2. Identity, type and "owner" of queue 0x3fcec294

- `ucQueueType == 3` = `queueQUEUE_TYPE_BINARY_SEMAPHORE`
  (FreeRTOS-Kernel/include/freertos/queue.h:83), produced by
  `xSemaphoreCreateBinary()` → `xQueueGenericCreate(1, 0, type 3)`
  (semphr.h:161). Matches `uxLength=1, uxItemSize=0`.
- The handle is recorded in the driver global
  `freeink::s_epdRefreshDone` (`EpdBus.cpp:74`, static `DRAM_ATTR SemaphoreHandle_t`,
  symbol `_ZN7freeinkL16s_epdRefreshDoneE @ 0x3fca1da8`, created at `EpdBus.cpp:158`).
  Runs 0041/0042 captured `sem_handle_ptr = 0x3fcec294` from live memory and set
  `factory_epd_semaphore_matches_blocked_queue = true`
  (`s3-x4pro-epd-isr-semaphore-0041.json`, `s3-x4pro-gpio6-pcpu-irq-0042-*.json`).
- **Binary semaphores do not track an owner.** `u.xSemaphore.xMutexHolder`
  (queue.c:93) is maintained only for mutex types; for type 3 it is stale heap
  bytes — do not interpret it. The functional "ownership" is code-defined:
  - Takers: only `EpdBus::waitRefreshComplete()` (0x4201e968..0x4201eabb).
    Blocking sites in the shipped binary: `0x4201e9e5` drain `take(0)`
    (EpdBus.cpp:437, cannot block), `0x4201ea15` `take(20)` (EpdBus.cpp:445),
    `0x4201ea6e` `take(30000)` inside
    `while (digitalRead(busy) != doneLevel)` (EpdBus.cpp:460).
  - Giver: `freeink::epdBusyIsr` (IRAM `0x40375604`) → `xSemaphoreGiveFromISR`
    (EpdBus.cpp:77-81), armed by `attachInterrupt(busy, epdBusyIsr, CHANGE)`
    at `0x4201e9fe` immediately before the first blocking take.
- `0x3fcbc460` (rendering mutex, held by the render task) is a *different* object;
  probe P6 reads its type/holder independently. If it is type 1/4 the
  `xMutexHolder` read there IS meaningful.

## 3. Why `xTaskGenericNotify` returned pdPASS without waking the task

Shipped `xTaskGenericNotify` (entry 0x40387444 = DWARF tasks.c:5903, i.e. the local
5.5.5 tree is line-exact for this function):

- tasks.c:5919-5921 — saves `ucOriginalNotifyState`, writes `taskNOTIFICATION_RECEIVED` (=2);
- tasks.c:5929-5931 — `eIncrement` bumps `ulNotifiedValue` 0→1;
- tasks.c:5971 — **only if `ucOriginalNotifyState == taskWAITING_NOTIFICATION` (=1)**
  does it `listREMOVE_ITEM(&pxTCB->xStateListItem)` (5973) and add to ready
  (5974). There is **no** "remove from event list / pending-ready list" path in this
  kernel variant.
- `xReturn` stays `pdPASS` regardless (5905) — pdPASS only proves the value was written.

Observed `ucNotifyState 0→2` proves the render task was **not** in
`xTaskGenericNotifyWait`/`ulTaskGenericNotifyTake` (those set 1 first;
tasks.c:5822-5830, and xTaskGenericNotifyWait entry 0x40387350 = line 5811).
It was blocked in `xQueueSemaphoreTake` (entry 0x4038473c = queue.c:1699), whose
wait places the TCB on **both** lists by design: `vTaskPlaceOnEventList` inserts
`xEventListItem` into the queue receive list (tasks.c:3767) and
`prvAddCurrentTaskToDelayedList` inserts `xStateListItem` into the delayed list
(tasks.c:3769); then `portYIELD_WITHIN_API()` (queue.c:1831). This is exactly the
observed "event list pvContainer=0x3fcec2b8 + stays on xDelayedTaskList1".

Conclusion: **task notifications cannot break a queue wait; the render pipeline is
gated on `s_epdRefreshDone` being given (or its timeout expiring), not on the
notification.** The QEMU cross-core IPI hypothesis is not needed to explain 0036.
On the 30 s branch the loop breaks after the timeout (`EpdBus.cpp:460`
`if (... != pdTRUE) break;`), so within the recorded 8-15 s post-tap windows the
task legitimately stays blocked — absence of a render is expected, and no SSD1677
frame can appear in that window regardless of IPI behavior.

## 4. Determining the exact wait site and caller stack (non-speculative recipe)

1. **Which take:** `wakeTick = (uint32_t*)(0x3fcec420+0x04)` minus
   `xTickCount @ 0x3fcabf94`. ≈20 → `EpdBus.cpp:445` (`0x4201ea15`);
   ≈30000 → `EpdBus.cpp:460` (`0x4201ea6e`). A 0 value is impossible (drain can't block).
2. **Stacked PC:** `pxTopOfStack @ TCB+0`; the tick-ISR thread frame is an
   `XtExcFrame` with `pc` at +0x04, `a0` at +0x0C (DWARF-verified on this ELF).
   Expect `pc ∈ [0x4038473c, 0x40384844]` (`xQueueSemaphoreTake` + 0xA9) — the task
   keeps running until the next tick switch — and `a0 ∈ {0x4201ea71, 0x4201ea18}`
   (EpdBus return sites). `info symbol` on both makes the claim checkable.
3. **Waiter list:** walk `hdr = 0x3fcec2b8`: first waiter = `*(void**)(hdr+0xC)`
   (that is `hdr->xListEnd.pxNext`; sentinel when equal to `hdr+8`).
   Each item: `pvOwner` item+0xC → TCB; `xItemValue` = event order,
   TCB+0x04 = wake tick.
4. **Interrupt arming check:** `GPIO_PIN6_REG = 0x6000408C` (confirmed
   `gpio_reg.h:541`; `int_type` bits [9:7], `int_ena` bits [17:13],
   `gpio_reg.h:31-35`). 0041/0037b recorded `gpio6_pin_cfg = 0` at sample time
   while the task was on the receive list → either the `attachInterrupt` register
   write never lands in the QEMU model, or the model does not implement GPIO
   PCFG read-back. That is the epd-busy/QEMU lane, but this state is now directly
   checkable with the probe (P8). Earlier concern that 0x6000408C was the wrong
   pin register is **retracted** — it is GPIO6 per S3 headers.

All steps are implemented as one read-only script: `0037-freertos-queue-tcb-probe.gdb`
(sections P1-P8; attach with
`xtensa-esp32s3-elf-gdb -batch -q firmware.elf -ex "target remote :<port>" -x 0037-freertos-queue-tcb-probe.gdb`).
Offline it self-validates every type/field/symbol reference (P1-P7 constants asserted
by the host test; live guest run only needed for values).

## 5. Evidence

- `readelf`: `xQueueSemaphoreTake=0x4038473c`, `xTaskGenericNotify=0x40387444`,
  `xTaskGenericNotifyWait=0x40387350`, `vTaskPlaceOnEventList=0x40386498`,
  `xDelayedTaskList1=0x3fcac00c`, `xDelayedTaskList2=0x3fcabff8`,
  `xTickCount=0x3fcabf94`, `_ZN7freeinkL16s_epdRefreshDoneE=0x3fca1da8`,
  `_ZN7freeink6EpdBus19waitRefreshCompleteEPKc=0x4201e968 (+355)`,
  `_ZN7freeinkL10epdBusyIsrEv=0x40375604`.
- `objdump` of `waitRefreshComplete`: three `callx8 a8→xQueueSemaphoreTake` sites
  with literal arguments `movi a11,0 / 20 / (48+0x7500)=0x7530=30000` at
  `0x4201e9e5/0x4201ea15/0x4201ea6e`; `__attachInterrupt` mode 3 (CHANGE) at
  `0x4201e9f6-0x4201e9fe`.
- DWARF: `Queue_t`/TCB/XtExcFrame offsets table above; line mapping kernel 5.5.5
  and SDK 44427f8 both hit (tests pass 4/4).
- QMP JSONs (host artifacts): 0041/0042 set
  `factory_epd_semaphore_matches_blocked_queue=true` with `sem_handle_ptr=0x3fcec294`.
- Note: `ybx-rtos-tcb-0037b.log` and `ybx-render-deadlock-0039.log` were **not
  present** in `diagnostics-20261030/` at analysis time; numbers quoted in the
  mission were re-derived from `s3-x4pro-tcb-0037b.json`,
  `s3-x4pro-epd-isr-semaphore-0041.json` and DWARF instead.

## 6. Recommended single next change (one step, owned by the epd/QEMU lane)

Make the guest-visible BUSY completion actually toggle GPIO6 into the emulated
GPIO PCFG interrupt path (extend the 0042 pcpu-edge QEMU experiment): before the
More tap, arm refresh; after the injected `DISPLAY_UPDATE` sequence, drive a
BUSY high→low edge on the emlined pin. Success criteria, all measurable by
`0037-freertos-queue-tcb-probe.gdb` + QMP without GDB breakpoints:
`0x6000408C` shows `int_ena!=0` while the render task is on the receive list;
after the edge `0x3fcec294+0x38 uxMessagesWaiting` returns to 0 via the ISR give,
the render TCB leaves 0x3fcec2b8 and 0x3fcac00c, `ucNotifyState` clears through
`xTaskGenericNotifyWait` (value consumed back to 0), and the SSD1677 RAM SHA /
frame counter increase in a **non-GDB** run — the standing acceptance gate.

## Files modified (this worktree only)

- `tools/ybx-system-emu/experiments/0037-agent-freertos-tcb.md` (this document)
- `tools/ybx-system-emu/experiments/0037-freertos-queue-tcb-probe.gdb` (read-only GDB probe)
- `tools/ybx-system-emu/experiments/test_0037_freertos_queue_mapping.py` (host mapping test)
