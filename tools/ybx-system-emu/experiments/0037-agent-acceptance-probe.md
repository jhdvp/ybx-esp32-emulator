# 0037 — acceptance probe: read-only FreeRTOS TCB / BUSY-semaphore telemetry

Date: 2026-10-30. Agent: acceptance-probe worktree (`agents/ybx-qwen38-acceptance-probe-20261030`).
Status: **host-side tooling complete, 138 host tests pass. No new guest run was performed**
(shared-Mac rule: no 40 s+ Docker guest runs by agents). Nothing here changes firmware,
QEMU, or any production tree. No navigation claim is made.

## Mission

Make `probe_x4pro_guest_touch_ui.py` collect, around one native GT911 More tap,
read-only QMP-HMP evidence of the render task TCB and of the semaphore at
`0x3fcec294`, and keep the success criteria strict. Fixed addresses are allowed
only behind the factory-flash SHA gate, and unavailable data must be marked as
unavailable.

## What was added

`freertos_guest_probe.py` (new, host-side, 490 lines + 344 lines of
`test_freertos_guest_probe.py`):

| Piece | Purpose |
| --- | --- |
| `parse_xp_words`, `GuestRam` | Decode `xp /<n>wx` replies by their per-line address headers (any QEMU wrapping), memoised, chunked in 8 words. A failed read is recorded in `errors` and returned as `None` — never as `0`. |
| `TCB`, `LIST_ITEM`, `LIST`, `QUEUE`, `KERNEL` | Offsets and kernel static addresses for the exact factory build (table below). |
| `enumerate_tasks`, `task_row` | Walk `pxReadyTasksLists[0..24]`, `xDelayedTaskList1/2`, `xPendingReadyList[0..1]` and read each task's name, priority, core, notification value/state and both list containers. **The render TCB address is discovered, not hardcoded.** Walks are bounded (`MAX_LIST_ITEMS=64`, cycle set, pointer-range checks). |
| `container_kind` | Names the kernel object that owns a container: `delayed-list-1`, `ready-priority-3`, `pending-ready-core-1`, or `queue@3fcec294.waiting-to-receive:binary-semaphore` (derived as `container - 0x24`, then structurally validated). |
| `read_task`, `read_queue`, `read_waiters` | Full record for the render task and for the object it is parked on, including the waiter list. A `Queue_t` is decoded only if `uc_queue_type` is a known type, `1 <= uxLength <= 4096`, `uxItemSize <= 64` and `uxMessagesWaiting <= uxLength`; the mutex-holder union word is reported only for mutex types, never for a binary semaphore. |
| `snapshot` | One observation: `xSchedulerRunning`, `pxCurrentTCBs[2]`, `freeink::s_epdRefreshDone` handle, GPIO BUSY line words (`GPIO_IN/STATUS/ENABLE/PIN6_CFG`), the whole task table, the render task, its block analysis, plus `available`/`reason`. |
| `assess_render_wake` | Pre/post classification: `notification_latched`, `blocked_on_binary_semaphore`, `blocked_object_is_epd_refresh_done`, `same_blocked_object`, `still_parked_in_delayed_list`, `render_task_woken`. `render_task_woken` is true only when the render task's own state container became a ready/pending-ready list or the task appears in `pxCurrentTCBs`. |

`probe_x4pro_guest_touch_ui.py`:

* `--rtos-diagnostics` (refuses to start unless the flash SHA-256 equals
  `206ed9661bd71477d1a54397c5a2cfe490179a3f59ab094f1e7421745c609523`),
  `--render-task-name` (default `ActivityManagerRender`), `--rtos-late-seconds`.
* Snapshots are taken before the press and after the release, plus an optional
  **late** sample (`rtos_late`, `late` panel frame) for the 30 s case below.
* New JSON keys: `rtos_before`, `rtos_after`, `rtos_late`,
  `rtos_render_wake_assessment`, `rtos_render_wake_assessment_late`,
  `factory_build_sha_gate_matched`, `guest_render_task_woken_after_contact`,
  `guest_ui_navigation_claim_allowed`, `guest_ui_navigation_claim_allowed_late`,
  `kernel_counters_are_not_screen_proof`.
* `assess_touch_to_ui` is unchanged. New `assess_navigation_claim(touch_gate, wake)`
  additionally **refuses** a navigation claim when the kernel state is measurable
  and the render task never left its blocked list, and stays silent (no invented
  refutation) when it is not measurable.

## Layout used, and how it was verified (all offline)

From the matching `.pio/build/x4pro/firmware.elf` (read-only DWARF queries with
`xtensa-esp32s3-elf-gdb`, kernel V10.5.1):

| Item | Offset / address |
| --- | --- |
| `tskTCB` size | 352 |
| `xStateListItem` / `xEventListItem` | `+0x04` / `+0x18` |
| `uxPriority` `pxStack` `pcTaskName` `xCoreID` | `+0x2C` `+0x30` `+0x34` `+0x44` |
| `uxTCBNumber` `uxTaskNumber` `uxBasePriority` `uxMutexesHeld` | `+0x4C` `+0x50` `+0x54` `+0x58` |
| `ulRunTimeCounter` | `+0x64` |
| `ulNotifiedValue[1]` / `ucNotifyState[1]` | `+0x158` / `+0x15C` |
| `ListItem_t` | `xItemValue +0`, `pxNext +4`, `pxPrevious +8`, `pvOwner +0xC`, `pvContainer +0x10` (size 20) |
| `List_t` | `uxNumberOfItems +0`, `pxIndex +4`, `xListEnd +8`; `xListEnd.pxNext` at `+0x0C` (size 20) |
| `Queue_t` (size 92) | `xTasksWaitingToSend +0x10`, `xTasksWaitingToReceive +0x24`, `uxMessagesWaiting +0x38`, `uxLength +0x3C`, `uxItemSize +0x40`, `uxQueueNumber +0x4C`, `ucQueueType +0x50` |
| Kernel statics | `xSchedulerRunning 0x3FCABF8C`, `xPendingReadyList 0x3FCABFC8` (`[2]`), `pxDelayedTaskList 0x3FCABFF4`, `xDelayedTaskList2 0x3FCABFF8`, `xDelayedTaskList1 0x3FCAC00C`, `pxReadyTasksLists 0x3FCAC020` (`[25]`), `pxCurrentTCBs 0x3FCAC214` (`[2]`) |
| EPD | `freeink::s_epdRefreshDone` handle `0x3FCA1DA8` (`EpdBus.cpp:74`) |

Independent cross-checks that the table describes the running guest:

* the recorded `xStateListItem.pvContainer = 0x3fcac00c` **is** `xDelayedTaskList1`;
* the recorded `xEventListItem.pvContainer = 0x3fcec2b8` **is** `0x3fcec294 + 0x24`,
  i.e. that queue's `xTasksWaitingToReceive` list — the task really is queued on it;
* `pxReadyTasksLists + 25*20 = 0x3fcac214 = pxCurrentTCBs`, and
  `pxDelayedTaskList + 4 = xDelayedTaskList2`, `+20 = xDelayedTaskList1`, `+20 = pxReadyTasksLists`
  (a host test pins these adjacencies so a future mismatch fails loudly);
* `ucQueueType = 3` = `queueQUEUE_TYPE_BINARY_SEMAPHORE`
  (`freertos/queue.h:83`, with BASE 0, MUTEX 1, COUNTING 2, RECURSIVE 4) together with
  `uxLength = 1`, `uxItemSize = 0`;
* `configMAX_TASK_NAME_LEN` is 16, so the kernel stores `ActivityManagerR` for
  `ActivityManagerRender` — matched by `task_matches`, not by a substring test.

`ucNotifyState` is reported **raw**, with the V10.x naming convention
(0 = no state, 1 = task waiting, 2 = notification recorded) attached as a label;
the enum is not present in this build's DWARF, so the number is the evidence,
the label is a hint.

## What this says about the 0036 finding

The tap chain and `xTaskGenericNotify` → `pdPASS` are already proven (0036).
This probe adds the reason the notification does not wake the render task:
`EpdBus::waitRefreshComplete()`
(`freeink-sdk/libs/display/FreeInkDisplay/src/bus/EpdBus.cpp:437-462`) drains
`s_epdRefreshDone`, attaches a `CHANGE` ISR on the BUSY pin and then blocks in
`xSemaphoreTake(s_epdRefreshDone, pdMS_TO_TICKS(30000))` while
`digitalRead(busy) != doneLevel`. A task blocked in a queue take is placed on
**both** the delayed list (state item) and the queue's receive list (event item)
— exactly the recorded state. So the render task is parked in an EPD BUSY wait,
not waiting for the `ActivityManager` notification; the notification is only
latched (`ulNotifiedValue` 0→1, `ucNotifyState` 0→2). Cross-core IPI/timer
delivery is not needed to explain this stall.

Correction to a reading made while preparing this probe: in the recorded runs
`GPIO_IN_REG = 0x609b`, and **bit 6 is 0 (LOW)**, not HIGH
(`0x609b & 0x40 == 0`); `GPIO_STATUS_REG = 0x00000000`, i.e. no GPIO edge was
ever latched, while QEMU's own panel property reports `busy = 0`. For an
active-low BUSY pin the guest therefore reads "still working" forever, the
completion ISR never runs, and the semaphore is never given. `pin6-config`
reads `0x00000000` because the current QEMU GPIO model does not implement
`GPIO_PINn_CFG_REG`; the level/edge words above are what that model does answer.

## How to run (human, Docker, no debugger)

```
--rtos-diagnostics --raw-x 350 --raw-y 760 \
--boot-seconds 39 --press-seconds 0.15 --after-seconds 10 --rtos-late-seconds 45
```

Decision table for the report JSON:

| Observation | Meaning |
| --- | --- |
| `rtos_*​.available = false` | decode refused (wrong build, read error, task unreachable) — nothing may be concluded |
| `notification_latched = true`, `render_task_woken = false`, `blocked_object_is_epd_refresh_done = true` | 0036 situation reproduced without a debugger: input reached FreeRTOS, BUSY wait still owns the task |
| `render_task_woken = true` + `guest_epd_frame_count_increased` + changed SSD1677 RAM SHA in `after`/`late` | real navigation, `guest_ui_navigation_claim_allowed = true` |
| frames increased but the render task is measurably still blocked | spontaneous refresh, claim refused |

Cost: one snapshot ≈ 43 HMP reads per 2 tasks in the unit image; on a real guest
(roughly 20–30 tasks) expect a few hundred small `xp` reads, i.e. seconds, with
no guest writes and no CPU halt. GDB stays unnecessary for this evidence.

## Host tests

`python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py'` → **138 tests OK**.

* `test_freertos_guest_probe.py` (24) — synthetic guest image built from the
  recorded addresses (`0x3fcec420`, `0x3fcec294`, `0x3fcac00c`, `0x609b`);
  covers HMP wrapping, error replies vs zeros, cache behaviour, discovery
  without a hardcoded TCB, container naming, waiter list, EPD-handle match,
  busy-bit decode, refusal on garbage/zeroed memory, wake vs still-blocked
  verdicts, and the DWARF adjacency pin.
* `test_probe_x4pro_guest_touch_ui.py` (15) — the existing splash-to-home and
  QMP-only rejections stay, plus: pixels + kernel wake allows the claim; a
  measurable still-blocked task refutes it; no pixels never allows it;
  unmeasurable kernel state does not invent a refutation; the diagnostic marker
  when `--rtos-diagnostics` is off; and `run()` raises on a non-factory flash
  **before** starting any emulator process.

## Recommended single next change

Make the virtual SSD1677 drive the **guest-visible BUSY GPIO line** instead of
only its internal `busy` property: connect the panel model's R/B# output to the
GPIO input injector for GPIO6 (edge pulse on `DISPLAY_START` … completion), so
the armed `CHANGE` ISR runs `epdBusyIsr` → `xSemaphoreGiveFromISR` and
`waitRefreshComplete()` returns. The quarantined experimental patch
`0042-s3-epd-busy-gpio-pcpu-irq.patch` already models `GPIO_STATUS`,
`GPIO_PCPU_INT`, `PINn_CFG` and edge latching, but nothing drives the line —
that wiring is the missing piece, and it is not part of `apply_qemu_patches.sh`.

Verify with this probe, in a non-GDB run: `render_task_woken = true`,
`blocked_object_is_epd_refresh_done` gone after the tap, `frames` increased and
the SSD1677 RAM SHA changed — only then raise `guest_ui_navigation_claim_allowed`.

## Files modified (this worktree only)

* `tools/ybx-system-emu/freertos_guest_probe.py` — new.
* `tools/ybx-system-emu/test_freertos_guest_probe.py` — new.
* `tools/ybx-system-emu/probe_x4pro_guest_touch_ui.py` — opt-in diagnostics,
  late window, navigation-claim gate, SHA gate.
* `tools/ybx-system-emu/test_probe_x4pro_guest_touch_ui.py` — new gate tests.
* `tools/ybx-system-emu/experiments/0037-agent-acceptance-probe.md` — this file.

No QEMU tree, no firmware, no SDK submodule, and no other worktree was touched.
