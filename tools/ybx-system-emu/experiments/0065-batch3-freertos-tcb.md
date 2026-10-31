# 0065 — batch 3: why app tasks were invisible, and the wake-tick probe that decides 20 ms vs 30 s

Lane: `freertos-tcb`. Branch `agents/ybx-emu-b3-freertos-tcb-20261031`.
Host-only work: no QEMU was built, no Docker was touched, no guest was started.
Everything measured below is either (a) a host-side decode of a synthetic guest
image, or (b) a symbol-table read of the factory ELF. Gap 2 stays a number to be
collected by the orchestrator; this lane supplies the instrument, not the
measurement.

Question answered: 0052 gap 1 — "app tasks (including the render task) were
still not reachable in the live snapshot, so
`guest_render_task_woken_after_contact=false` must be read as *not measurable*"
— and gap 2 — "no report measured `wakeTick = TCB+0x04 − xTickCount` to close
out the 20 ms vs 30 s question".

## 1. Gap 1 root cause: the walker never visited the list the tasks are on

`uxTaskGetSystemState` — the kernel's own task enumeration — walks five
containers. The shipped walker walked three.

Verified in `/Users/yaraslauhancharou/.platformio/packages/framework-espidf/`
(the IDF source copy; the shipped object comes from a prebuilt Arduino archive —
see §5 item 3 for what that does and does not allow me to claim):

| file:line | content | walked by the old probe? |
|:--|:--|:--|
| `components/freertos/FreeRTOS-Kernel/tasks.c:2986` | `prvListTasksWithinSingleList(…, &( pxReadyTasksLists[ uxQueue ] ), eReady)` | yes |
| `…/tasks.c:2991-2992` | `pxDelayedTaskList`, `pxOverflowDelayedTaskList` | yes |
| `…/tasks.c:2998` | `prvListTasksWithinSingleList(…, &xTasksWaitingTermination, eDeleted)` | **no** |
| `…/tasks.c:3006` | `prvListTasksWithinSingleList(…, &xSuspendedTaskList, eSuspended)` | **no** |

Same lines in the SMP copy: `components/freertos/FreeRTOS-Kernel-SMP/tasks.c:4428`
(`xTasksWaitingTermination`) and `:4436` (`xSuspendedTaskList`).

Why that is the whole gap and not a corner case — `prvAddCurrentTaskToDelayedList`
routes an indefinite wait to a *different* list and gives it **no wake time at
all**:

```
…/FreeRTOS-Kernel/tasks.c:6381   if( ( xTicksToWait == portMAX_DELAY ) && ( xCanBlockIndefinitely != pdFALSE ) )
…/FreeRTOS-Kernel/tasks.c:6386       listINSERT_END( &xSuspendedTaskList, &( pxCurrentTCBs[ xCurCoreID ]->xStateListItem ) );
…/FreeRTOS-Kernel/tasks.c:6393   xTimeToWake = xConstTickCount + xTicksToWait;   /* the else-branch */
```

SMP equivalents: `:8460`, `:8465` (`pxCurrentTCB`, singular), `:8472`.

A booted reader parks its application tasks in `xSemaphoreTake(…, portMAX_DELAY)`
/ notification waits. Those TCBs live on `xSuspendedTaskList`, are not in
`pxReadyTasksLists`, are not in `pxCurrentTCBs`, and are not on any delayed
list. IDLE0/IDLE1 (suspended by the scheduler, but self-removed) and `loopTask`
(the one task that runs) were visible; everything else was structurally
unreachable. So `guest_render_task_woken_after_contact=false` in 0052 was
correctly reported as *not measurable*.

`xSuspendedTaskList` and `xTasksWaitingTermination` were already in the recorded
`KERNEL` table (`freertos_guest_probe.py:36-37`); nothing read them.

### Confirmation that the pinned build exposes these symbols

Real run against the factory ELF (read-only evidence,
`.pio/build/x4pro/firmware.elf` under
`/Users/yaraslauhancharou/.local/state/xteink-agent-workspaces/fw-092c2be5-symbols-20261029`):

```
$ python3 - <<'PY'   # from tools/ybx-system-emu
import freertos_guest_probe as rtos
lay = rtos.KernelLayout.from_elf("/Users/…/fw-092c2be5-symbols-20261029/.pio/build/x4pro/firmware.elf")
print(lay.to_dict()); print([k for k,v in rtos.KERNEL.items() if lay.addr(k)!=v])
PY
elapsed_ms 46.8
{'addresses': {'pxCurrentTCBs': '0x3fcac214', 'pxDelayedTaskList': '0x3fcabff4',
 'pxReadyTasksLists': '0x3fcac020', 'xDelayedTaskList1': '0x3fcac00c',
 'xDelayedTaskList2': '0x3fcabff8', 'xNextTaskUnblockTime': '0x3fcabf74',
 'xPendingReadyList': '0x3fcabfc8', 'xSchedulerRunning': '0x3fcabf8c',
 'xSuspendedTaskList': '0x3fcabf9c', 'xTasksWaitingTermination': '0x3fcabfb4',
 'xTickCount': '0x3fcabf94'},
 'cores': 2, 'epd_refresh_done': '0x3fca1da8', 'max_priorities': 25,
 'source': 'discovered from /Users/…/x4pro/firmware.elf'}
kernel statics differing from recorded table: []
pxCurrentTCBs: [(1070252564, 8)]
epd symbol: [(1070210472, 4)]
```

Every recorded address is reproduced from the ELF, not copied into it; `max_priorities`
25 and `cores` 2 are *derived* from symbol sizes
(`pxReadyTasksLists` size ÷ `LIST_SIZE`, `xPendingReadyList` size ÷ `LIST_SIZE`,
cross-checked against `pxCurrentTCBs` size ÷ 4), see
`freertos_guest_probe.py:227-287`. This is the "discover the layout, do not
hardcode an address" requirement: `KernelLayout.from_elf` is the primary path and
`KernelLayout.recorded()` (`:221-225`) exists only so a caller without the ELF can
still run, with the refusal order documented below.

## 2. What changed in the code

### `freertos_guest_probe.py` (882 lines)

- `scheduler_lists` (`:624`) now enumerates `xSuspendedTaskList` and
  `xTasksWaitingTermination` next to the ready array, the delayed pair and the
  per-core pending lists. The docstring names the two kernel lines it is mirroring.
- `walk_list` (`:647`) walks any `List_t` head with non-fatal per-item skips and
  counts anomalies per container: `items_walked`, `duplicate-owner`,
  `implausible-owner`, `unreadable-owner`, `undecodable-task`, plus the
  `read-error` / `never-initialised` flags. A list that yields nothing but raised
  one surprise still reaches the snapshot (`WALK_REPORT_BASE`/`WALK_QUIET_FLAGS`,
  `:87-92`).
- `enumerate_tasks` (`:697`) returns the decoded task set across all containers,
  deduplicated by TCB, capped by `MAX_ENUMERATED_TASKS`/`MAX_LIST_ITEMS`.
- `decode_wake` (`:494-534`) reads the state-list item and classifies it:
  a suspended task gets `wake_tick_meaning =
  "no-wake-tick-indefinite-block-tasks-c-6381-6386"` and
  `wake_ticks_remaining = None` rather than an invented number; a delayed task
  gets `wake_ticks_remaining = (xTimeToWake − xTickCount)` with the elapsed case
  reported as `0` instead of a 4-billion wrap; a ready task's stale
  `xItemValue` is not read as a time at all.
- `classify_epd_wait` (`:293-315`) maps remaining ticks onto the two branches
  pinned from the factory disassembly of `freeink::EpdBus::waitRefreshComplete`
  (`0x4201e968`): take sites `0x4201ea15` / `0x4201ea6e`
  (`EPD_WAIT_BRANCHES`, `:115-120`).
- `KernelLayout.from_elf` refuses instead of correcting. Order: missing symbol →
  ambiguous symbol → word size ≠ 4 → `List_t` size 0 or not a multiple of 20 →
  `pxCurrentTCBs` 0 or not a multiple of 4 → derived bounds (pointer/cores
  mismatch, then 1..256 priorities / 1..8 cores) → DRAM word-alignment per key.
  The EPD handle is taken from the ELF only when exactly one word-aligned DRAM
  entry exists for `_ZN7freeinkL16s_epdRefreshDoneE`; otherwise the source string
  says the handle stays recorded.
- `snapshot` (`:745`) and `assess_render_wake` (`:826-882`) carry the new state
  through; `assess_render_wake` separates "left the indefinite block" from
  "is running" from "re-parked on a delayed list", and reports `render_task_woken`
  only for the running/ready/pending-ready cases (`:838-843`).

Not a UI claim: nothing in this module touches or reads the framebuffer.

### `probe_x4pro_freertos_wake.py` (321 lines, new — for the orchestrator to run)

CLI that answers gap 2 with a number. Read-only `xp /Nwx` against guest RAM plus
one real GT911 contact through the existing QOM properties.

- Hard gates before anything attaches: flash SHA-256 must equal
  `206ed9661bd71477d1a54397c5a2cfe490179a3f59ab094f1e7421745c609523`
  (`FACTORY_092C2BE5_FLASH_SHA256`, `freertos_guest_probe.py:17`), or exit 2 with
  "Refusing to decode"; `--elf` layout discovery failure exits 2 likewise.
- Sampling: `before` snapshot → `ybx-touch-x`, `ybx-touch-y`,
  `ybx-touch-state=1` → `--press-seconds` → `state=0` → N post-contact snapshots.
  The first post-contact sample is the decisive one; it catches the wait while it
  is still running.
- `verdict` (`:146`) returns `epd-busy-poll-20ms`, `epd-busy-timeout-30s`,
  `ambiguous` (both windows still fit the remaining time — "sample earlier or
  faster"), or `not-measured` with the reason.
- `tick_rate_check` (`:124`) measures `xTickCount` against wall clock so the
  1000 Hz assumption implied by the folded `pdMS_TO_TICKS` immediates is checked
  rather than asserted. With `--sample-interval 0` it is meaningless by
  construction; run it with real spacing.
- Framebuffer honesty: `not_a_framebuffer_claim` is in the report JSON.

Recipe (the lane that wrote this never started a guest):

```sh
docker cp tools/ybx-system-emu/probe_x4pro_freertos_wake.py \
  ybx-x4pro-browser-8784:/tests/probe_x4pro_freertos_wake.py
docker cp tools/ybx-system-emu/freertos_guest_probe.py \
  ybx-x4pro-browser-8784:/tests/freertos_guest_probe.py
docker exec ybx-x4pro-browser-8784 sh -c 'cd /tests && python3 \
  probe_x4pro_freertos_wake.py \
  --qmp "$(ls -d /tmp/ybx-x4pro-browser-*/qmp.sock | head -1)" \
  --flash /work/s3-flash-complete-rom-test.bin \
  --contact more --samples 6 --sample-interval 0.25 \
  --out /tmp/freertos-wake.json'
docker cp ybx-x4pro-browser-8784:/tmp/freertos-wake.json .
```

Both files must be copied — the probe imports `freertos_guest_probe`. Add
`--elf /path/to/x4pro/firmware.elf` to discover the layout instead of using the
recorded table; the report records which path ran in `layout_source`.

## 3. Tests: `test_freertos_guest_probe.py` (1303 lines, 82 tests, no guest, no network)

New coverage in this lane, by class (`test_freertos_guest_probe.py:447`,
`:521`, `:612`, `:726`, `:836`, `:976`, `:994`, `:1040`, `:1154`, `:1257`):

- `IndefiniteBlockDiscoveryTests` — render task found on `xSuspendedTaskList`;
  `xTasksWaitingTermination` decoded as a separate container; a suspended block
  reports *no* wake tick instead of a number; zeroed list flagged
  `never-initialised` and not mistaken for a task; empty initialised lists stay
  quiet; and the negative case — a task that is neither running nor on any list
  stays unreachable, no fabricated render task from a broken image.
- `WakeTickDecodeTests` — 30000 separated from 20; 20 alone does not single out a
  branch; remaining > 30000 is not an EPD busy wait; elapsed wake tick is not
  wrapped into a huge remaining time; exactly-reached wake time reports 0; stale
  `xItemValue` on ready and suspended tasks is not read as a time; the second
  delayed list is a delayed list; missing `xTickCount` is read from the guest;
  unreadable state item yields no tick.
- `EpdWaitClassificationTests` — classification table and the branch labels cite
  the source lines they came from.
- `TaskListWalkReportTests` — anomaly counters, including the owner-hole case
  through `qmp_with_owner_hole` (`:424`).
- `ElfLayoutDiscoveryTests` + `WrongBuildRefusalTests` — layout discovery against
  a synthetic ELF whose addresses deliberately differ from `rtos.KERNEL`
  (`LAYOUT_SYMBOLS`, `:764`), plus every refusal path: missing symbol, ambiguity,
  bad word size, non-`List_t` size, bad `pxCurrentTCBs`, implausible derived
  bounds, non-DRAM addresses, shifted build.
- `FactoryElfCrossCheckTests` — the real factory ELF reproduces the recorded
  table (skipped if the ELF is absent from the machine).
- `WakeProbeTests` / `WakeProbeCliGateTests` / `WakeProbeGuestSimulationTests` —
  the delivered CLI's helpers, its gates (asserting `qmp_connect` is *never*
  called: `assertFalse(connect.called, "the SHA gate must run before any
  connection")`), and `main()` end-to-end against a simulated guest whose memory
  swaps when the finger lifts.

Real output, verbose, the classes that carry the two gaps:

```
$ python3 -m unittest -v test_freertos_guest_probe.FactoryElfCrossCheckTests \
    test_freertos_guest_probe.IndefiniteBlockDiscoveryTests \
    test_freertos_guest_probe.WakeTickDecodeTests \
    test_freertos_guest_probe.EpdWaitClassificationTests \
    test_freertos_guest_probe.WakeProbeGuestSimulationTests 2>&1 | tail -40
test_factory_elf_yields_the_recorded_kernel_layout ... ok
test_a_task_that_is_neither_running_nor_listed_stays_unreachable ... ok
test_initialised_empty_lists_are_quiet_in_the_walk_report ... ok
test_render_task_is_found_on_the_suspended_list ... ok
test_suspended_block_reports_no_wake_tick_instead_of_a_number ... ok
test_waiting_termination_is_a_separate_container ... ok
test_zeroed_list_is_flagged_never_initialised_not_as_a_task ... ok
test_a_task_that_leaves_the_suspended_list_picks_up_a_wake_tick ... ok
test_elapsed_wake_tick_is_not_wrapped_into_a_huge_remaining_time ... ok
test_missing_tick_count_is_read_from_the_guest ... ok
test_ready_task_stale_item_value_is_not_read_as_a_time ... ok
test_remaining_time_above_both_windows_is_not_epd_busy_wait ... ok
test_suspended_task_stale_item_value_is_not_read_as_a_time ... ok
test_the_second_delayed_list_is_a_delayed_list_too ... ok
test_thirty_second_timeout_is_separated_from_the_twenty_ms_poll ... ok
test_twenty_ms_timeout_alone_does_not_single_out_a_branch ... ok
test_unreadable_state_item_yields_no_tick ... ok
test_wake_time_exactly_reached_is_reported_as_zero ... ok
test_branches_cite_the_source_lines_they_came_from ... ok
test_classification_table ... ok
test_main_measures_the_thirty_second_window_after_one_contact ... ok
test_skip_contact_leaves_the_wake_question_unanswered ... ok
----------------------------------------------------------------------
Ran 22 tests in 0.131s

OK
```

Verification gate, whole directory, real output:

```
$ cd /Users/yaraslauhancharou/.local/state/xteink-agent-workspaces/ybx-emu-batch3-20261031/freertos-tcb \
  && python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q
----------------------------------------------------------------------
Ran 345 tests in 12.090s

OK (skipped=7)
```

12.1 s, no Docker, no network, no guest. The 7 skips are other lanes' tests that
need a firmware checkout or a compiled QEMU.

## 4. Simulated end-to-end run of the delivered probe (host, synthetic image)

Not a measurement of the real guest. `build_image(render_state_list=SUSPENDED)`
before the contact and `build_image(render_wake_tick=30500, tick_count=1000)`
after, `xTickCount = 1000` → `30500 − 1000 = 29500` remaining, which only the 30 s
branch can leave behind. Real stdout of `main()` through the CLI:

```
exit: 0
samples: [('before',  True, tick=0,    remaining=None,  branch=None,               candidates=None),
          ('after-1', True, tick=1000, remaining=29500, branch='epd-busy-timeout-30s',
                                            candidates=['epd-busy-timeout-30s']),
          ('after-2', True, tick=1000, remaining=29500, branch='epd-busy-timeout-30s',
                                            candidates=['epd-busy-timeout-30s'])]
verdict: {'answer': 'epd-busy-timeout-30s', 'epd_wait_branch': 'epd-busy-timeout-30s',
          'epd_wait_candidates': ['epd-busy-timeout-30s'],
          'reason': 'only-one-measured-window-is-long-enough',
          'render_task_reachable': True, 'render_task_woken_first_after': False,
          'wake_ticks_remaining_after_max': 29500}
render_task_woken_after_contact: {'available': True, 'left_indefinite_block': True,
  'render_task_state_list_before': 'suspended-list',
  'render_task_state_list_after': 'delayed-list-1',
  'wake_ticks_remaining_before': None, 'wake_ticks_remaining_after': 29500,
  'wake_tick_meaning_before': 'no-wake-tick-indefinite-block-tasks-c-6381-6386',
  'wake_tick_meaning_after': 'absolute-wake-tick-minus-xTickCount',
  'still_parked_in_delayed_list': True, 'still_parked_indefinitely': False,
  'blocked_object_is_epd_refresh_done': True, 'same_blocked_object': True,
  'render_task_woken': False}
task_list_walk[0]: {'phase': 'before', 'read_errors': [], 'walk':
  {'ready-priority-3':   {'items_walked': 1, 'duplicate-owner': 0, 'implausible-owner': 0,
                          'unreadable-owner': 0, 'undecodable-task': 0},
   'suspended-list':     {'items_walked': 1, 'duplicate-owner': 0, 'implausible-owner': 0,
                          'unreadable-owner': 0, 'undecodable-task': 0}}}
injection order: [('ybx-touch-x', 350), ('ybx-touch-y', 760),
                  ('ybx-touch-state', 1), ('ybx-touch-state', 0)]
layout_source: 'recorded factory 092c2be5 statics (no --elf given)'
```

The gap-1 assertion in `test_main_measures_the_thirty_second_window_after_one_contact`
(`test_freertos_guest_probe.py:1154`) is the before-snapshot: `available=True`
with the render task decoded off `xSuspendedTaskList`, which the shipped walker
never reached. `render_task.name` is `ActivityManagerR`, not `ActivityManagerRender`
— `configMAX_TASK_NAME_LEN` is 16 (`experiments/0037-agent-acceptance-probe.md:78`);
`task_matches` (`freertos_guest_probe.py:736-742`) tolerates the truncation.

`render_task_woken=false` here is the *synthetic* image's after-state: the task
re-entered a timed wait (delayed-list-1) rather than being caught running. That is
the signature of a re-arming wait loop and is exactly why the probe samples
immediately after release; it is not a claim about the real guest.

## 5. What is proven, what is not

Proven on this host:

1. Gap 1's mechanism, from kernel source (`tasks.c:6381/6386/6393`, `:2998/:3006`)
   and from the pinned factory ELF's symbol table. The two lists the kernel walks
   and the old probe did not.
2. The extended decoder reaches a render task parked on `xSuspendedTaskList` and
   decodes a `portMAX_DELAY` block as "no wake tick", never as a number.
3. Layout is discovered, not hardcoded: the real factory ELF reproduces every
   recorded static, `max_priorities`/`cores` derived from symbol sizes, 46.8 ms.
4. A wrong build is refused at the CLI before any connection is attempted
   (`WakeProbeCliGateTests`), and at the layout layer before any word is read.

Not proven — stated plainly, not estimated:

1. **The 20 ms vs 30 s answer for the real guest is UNMEASURED.** No wake-tick
   number from a running emulator exists in this lane. Until the orchestrator runs
   §2's command, 0052 gap 2 stays open. `classify_epd_wait` only turns a measured
   remaining-tick into a branch label; the synthetic `29500` in §4 is a test
   fixture, not evidence.
2. **No gap-1 live snapshot JSON exists on this host.** "Reachable in a live
   snapshot" is demonstrated against a synthetic guest image only. The real check
   is the orchestrator's `--rtos-diagnostics` run: application tasks must appear in
   `task_list_walk` beyond IDLE/loopTask.
3. **Kernel variant is not pinned by this lane.** `components/freertos/CMakeLists.txt:33-34`
   selects `kernel_impl = "FreeRTOS-Kernel-SMP"` on `CONFIG_FREERTOS_SMP`;
   `firmware.map:379` records only
   `framework-arduinoespressif32-libs/esp32s3/dio_opi/libfreertos.a(tasks.c.obj)`
   — a prebuilt Arduino archive, so the `tasks.c` line numbers quoted above are
   from the IDF source copy under `framework-espidf`, which is the same kernel
   family but not a proof of the exact shipped revision. What *is* checkable from
   the artifact itself: the factory ELF exposes `pxCurrentTCBs` (plural, 8 bytes =
   2 pointers), the naming of `FreeRTOS-Kernel/tasks.c:6386`
   (`pxCurrentTCBs[xCurCoreID]`), whereas `FreeRTOS-Kernel-SMP/tasks.c:8465` uses
   singular `pxCurrentTCB`. A singular-named build is therefore refused by
   `from_elf` rather than decoded with borrowed offsets. Absence of
   `vTaskCoreAffinityGet/Set` is not proof of anything — `grep -c` over the map
   returns 0, and `--gc-sections` removes unreferenced functions.
4. **The guest cannot self-enumerate.** `uxTaskGetSystemState` is absent from the
   factory ELF object symtab (`read_elf_object_symbols` → `None`), which is why a
   host-side walker is needed at all.
5. `configMAX_TASK_NAME_LEN` truncation means the render task is matched by prefix;
   a second task whose name shares the first 16 characters would collide. Not
   observed, not excluded.
6. Tick rate 1000 Hz is an assumption derived from the folded
   `pdMS_TO_TICKS(20)`/`pdMS_TO_TICKS(30000)` immediates; `tick_rate_check` exists
   to corroborate or refute it against a live guest. Run it with real
   `--sample-interval` spacing, otherwise the rate is garbage.
7. Panel correctness is out of scope for this lane and untouched: `pvContainer`
   based conclusions and framebuffer SHAs stay with
   `probe_x4pro_guest_touch_ui.py`. Wi-Fi radio, physical waveform, sleep/resume
   remain unemulated.

## 6. Risks

- **Decoding a foreign build.** Two independent gates: flash SHA at the CLI, symbol
  checks in `KernelLayout.from_elf`. Both refuse; neither guesses. The residual risk
  is a build that shares the flash SHA but not the ELF — impossible by construction,
  but a stale `--elf` path silently falling back to recorded statics would not be
  caught; `layout_source` in the report is the field to read.
- **List-walk loops.** A corrupted `pxNext` could spin; bounded by
  `MAX_LIST_ITEMS` (64) per container and `MAX_ENUMERATED_TASKS` (96) overall, with
  the anomaly counted rather than hidden.
- **Reading a running guest.** `xp` is read-only, but snapshots are not atomic: the
  wake tick can change between the item read and the `xTickCount` read, which is why
  the verdict takes the *maximum* remaining time over the post-contact samples and
  reports ambiguity instead of a confident branch when both windows still fit.
- **Touch injection has side effects** on the orchestrator's live instance (one real
  GT911 contact through `qom-set`). `--skip-contact` gives the passive before/after
  pair, which cannot answer the branch question — the report says so rather than
  inventing an answer.

## 7. Files touched

- `tools/ybx-system-emu/freertos_guest_probe.py` — extended (882 lines)
- `tools/ybx-system-emu/probe_x4pro_freertos_wake.py` — new (321 lines)
- `tools/ybx-system-emu/test_freertos_guest_probe.py` — extended (1303 lines, 82 tests)
- `tools/ybx-system-emu/experiments/0065-batch3-freertos-tcb.md` — this report

No shared file (`README.md`, `QEMU_BRINGUP.md`, `experiments/README.md`, other
lanes' scripts) was modified. Standard library only.
