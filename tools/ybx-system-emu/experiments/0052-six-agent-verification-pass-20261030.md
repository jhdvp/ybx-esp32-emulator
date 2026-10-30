# 0052 — verification pass over both six-agent batches (2026-10-30)

Question answered: was every agent's work checked, in both iterations, against
the code and against a running emulator rather than against the reports?

Method: read all 14 reports, re-run the host suites, re-run the device-level
QTest probes against the current rebuilt QEMU (`f07744d9…`, chain 0042 + 0043 +
0037 + 0051), and re-run the factory guest through the browser and the native
touch probe.

## Batch 1 — six Qwen3.8-Flash agents (0037 / 0043)

| Agent | Contribution in tree | Claim | How it stands now |
|:--|:--|:--|:--|
| acceptance-probe | `freertos_guest_probe.py`, `test_freertos_guest_probe.py`, `--rtos-diagnostics` / `--rtos-late-seconds` + flash-SHA gate in `probe_x4pro_guest_touch_ui.py` | tooling to read TCB/BUSY state without inventing pixels | **Code confirmed.** Live run on the current binary: `factory_build_sha_gate_matched=true`, `true_guest_touch_to_ui_verified=true`. The TCB half of its diagnosis is **not** reproduced (see gap 1). |
| crosscore-ipi | `crosscore_ipi_verdict.py` + tests, `0043-*.gdb`, `0043-s3-optin-crosscore-ipi-counters.patch` | a lost FROM_CPU IPI is not the root cause | **Chain confirmed** in QEMU source; **counters confirmed only synthetically** by QTest (routed → CPU IRQ 5, unrouted counter moves on IRQ 16). 0050 already corrected the agent's reading of `from-cpu-asserted`. |
| epd-busy | `0043-agent-epd-busy-model-test.c`, review of 0042 | shipped model leaves BUSY low, so `waitRefreshComplete` takes the 20 ms fallback; multi-second "hang" snapshots were GDB-halt artefacts | **Confirmed twice.** Recompiled and ran the host model: `shipped wake=20 irqs=0`, `0042 wake=351 irqs=1 acks=1`, `no-PCPU storm`. Live guest snapshot shows GPIO6 `input_bit=0`, `interrupt_status_bit=0`. This contradicts the 30 s framing used by two other reports. |
| freertos-tcb | `0037-freertos-queue-tcb-probe.gdb`, `test_0037_freertos_queue_mapping.py` | queue `0x3fcec294` is `s_epdRefreshDone`; a notification cannot break a queue wait | **Offsets and reasoning confirmed** (4 mapping tests + code reading). **Live state unconfirmed**: the raw GDB logs behind `s3-x4pro-tcb-0037*.json` are not in the tree, and those JSONs contain only panel/touch fields. |
| qemu-epd-fix | `test_x4pro_epd_busy_semantics.py`, `0037-…edge-telemetry.patch` | BUSY semantics + edge telemetry | **Re-run and confirmed** on the rebuilt binary (220 MMIO commands, `gpio6_level_irq_follows_epd_busy=true`). Its telemetry patch carried the reset-ordering defect, fixed by 0051. |
| render-pipeline | report + `0037-render-busy-poll-check.py` | stuck in an untimed `waitBusy(UcIdleHigh)` poll | **Rejected** and stays rejected: the branch needs UC8179/UC8279, the X4 Pro panel is SSD1677, and `pvContainer=0` conflicts with the observed queue container. Archive only. |

## Batch 2 — six Qwen3.8-Flash agents (0048)

| Agent | Status after verification |
|:--|:--|
| epd-lifecycle | Finding **confirmed live** and fixed: reset during BUSY-high produced `rise=0, fall=1, high_ms=620`, now `0, 0, 0` (0051). |
| browser-resilience | **Confirmed**: `/api/reboot` on the running instance returned the guest to the Home frame SHA. |
| gui-navigation | Tooling **confirmed**; its own live chain was not executed by the agent. Executed here: Home → More → YBX Settings → System settings → Back with the accepted SHAs. The report's tap coordinates were wrong; the working ones are `(350,760) (240,453) (210,679) (35,61)`. |
| reproducible-build | **Confirmed**: `check_qemu_patch_composition.py guardrails --require-artifacts` → `ok=true` against the live checkout, exact factory flash SHA, current binary SHA recorded. |
| wifi-radio | **Documentation only**, correctly labelled: radio windows zero-fill, no scan/association/DHCP claimed or produced. |
| wifi-flow | **Tests only**: 24 state-machine tests compile the production `.cpp`; they skip without a firmware checkout (287 tests standalone vs 312 with it). |

## Gaps that remain open

1. **Live TCB state is still unproven.** The read-only probe enumerates ready,
   delayed and pending lists; a task blocked on a queue *without* a timeout, or
   one caught running, was invisible. The running-task case is fixed in this
   pass; app tasks (including the render task) were still not reachable in the
   live snapshot, so `guest_render_task_woken_after_contact=false` must be read
   as "not measurable", not as "did not wake".
2. **20 ms vs 30 s branch** is decided in favour of 20 ms for the shipped model,
   but no report measured `wakeTick = TCB+0x04 − xTickCount` to close it out.
3. **0043 IPI counters** are validated by QTest only; the browser API cannot read
   QOM properties, so no in-guest measurement exists.
4. Historical QA JSONs (`s3-x4pro-tcb-0037*`, `0033`, `0035`) were produced on
   older binaries (`27179636…`) and record no `rtos_*` fields; they cannot be
   re-derived from what is in the tree.
5. Physical E-Ink waveform, sleep/resume and Wi-Fi remain unemulated.

## Reproduce

```sh
cd ~/Projects/YBX/ybx-esp32-emulator
python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py'   # 312 OK, 6 skip
YBX_FIRMWARE_ROOT=/nonexistent \
  python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py'  # 288 OK, 7 skip
cc -O0 -Wall -Wextra tools/ybx-system-emu/experiments/0043-agent-epd-busy-model-test.c -o /tmp/m && /tmp/m
python3 tools/ybx-system-emu/check_qemu_patch_composition.py guardrails \
  --qemu-root ~/.local/state/ybx-emulator-upstream/qemu-0043-agent-audit-20261030 \
  --flash <qa>/s3-flash-complete-rom-test.bin --rom <qa>/s3-full-rom-generator-test.bin \
  --qemu-bin <qemu>/build-ybx/qemu-system-xtensa --require-artifacts
```

QTest probes (`probe_x4pro_epd_qtest.py`, `probe_x4pro_touch_injection_qtest.py`,
`probe_x4pro_telemetry_qtest.py`, `probe_x4pro_guest_touch_ui.py`) run inside
`ybx-qemu-builder:20261029` with `--network none`; see `QEMU_BRINGUP.md`.
