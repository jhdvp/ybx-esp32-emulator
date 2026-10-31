# 0069-epd-waveform-apply — the 0061 SSD1677 model, applied, built, measured

Date: 2026-10-31. Lane: waveform-apply (batch 4, six-agent orchestration).
Proposal consumed: experiments/0061-batch3-epd-waveform.md +
experiments/0061-ssd1677-waveform-sleep.patch (never compiled in batch 3).
Pinned source of record: `/Users/yaraslauhancharou/.local/state/ybx-emulator-upstream/qemu-0043-agent-audit-20261030`
(chain 0042+0043+0037+0051, uncommitted in its own worktree; READ-ONLY here —
never patched, never built against). Everything below ran on a private copy.

## 1. What this lane did

1. `cp -a` of the audit checkout → `qemu-0069-waveform-apply-20261031`,
   applied 0061's patch there, rebuilt `qemu-system-xtensa` with ninja -j2 in
   `ybx-qemu-builder:20261029`, and ran
   `tools/ybx-system-emu/probe_x4pro_epd_waveform_qtest.py` against the result.
2. Fixed four real bugs the compiled run exposed (probe harness ×2, probe CLI
   contract ×1, host-mirror divergence ×1) and added one device-side
   improvement (QOM read aliases for the window properties) → shipped as
   `experiments/0069-ssd1677-waveform-sleep-v2.patch`.
3. Extended `test_x4pro_epd_waveform.py` only with cases the compiled run
   measured; no new duration was invented (see §6/§7).

The shared tree was only ever touched with `git apply --check` (read-only).
The live emulator container `ybx-x4pro-browser-8784` was not touched.

Provenance:
- v2 patch file SHA-256 `9cdb2fbe8be55d9096dda32d9170a0db23a554f833173a2c10670568dd117757`, 211 lines.
- rebuilt binary `/Users/yaraslauhancharou/.local/state/ybx-emulator-upstream/qemu-0069-waveform-apply-20261031/build-ybx/qemu-system-xtensa`
  SHA-256 `7186bf9ce2d241f33ebbc73ac526513abce2228a0b2ca2796fa5e6ed1fc103c8`.
- probe machine inputs: factory ROM `qa-20261029/s3-full-rom-generator-test.bin`
  SHA-256 `5b950be9…` (verified by `shasum -a 256` before the run), supplied as
  `-L …/esp32s3_rev0_rom.bin` exactly like the other qtest probes.
  The factory flash image `s3-flash-complete-rom-test.bin`
  (`206ed966…`) is **not** consumed by these measurements: under
  `-accel qtest` the CPU never executes, so no flash content can influence the
  model windows. Guest-level (flash-booting) verification of the patched
  model is explicitly unproven here — §7.

## 2. Copy, apply, build (real commands and output)

```
$ cp -a ~/.local/state/ybx-emulator-upstream/qemu-0043-agent-audit-20261030 \
        ~/.local/state/ybx-emulator-upstream/qemu-0069-waveform-apply-20261031   # 608 MB
$ git -C qemu-0069-waveform-apply-20261031 apply --check -v <lane>/experiments/0061-ssd1677-waveform-sleep.patch
Checking patch hw/display/ybx_ssd1677.c...
Checking patch include/hw/display/ybx_ssd1677.h...
Checking patch hw/xtensa/esp32s3.c...            # exit 0
$ git -C qemu-0069-waveform-apply-20261031 apply -v …/0061-ssd1677-waveform-sleep.patch
Applied patch hw/display/ybx_ssd1677.c cleanly.
Applied patch include/hw/display/ybx_ssd1677.h cleanly.
Applied patch hw/xtensa/esp32s3.c cleanly.
$ docker run --rm --platform linux/arm64 \
    -v "$HOME/.local/state/ybx-emulator-upstream/qemu-0069-waveform-apply-20261031:/src" \
    -w /src/build-ybx ybx-qemu-builder:20261029 ninja -j2 qemu-system-xtensa
[1/6] Generating qemu-version.h with a custom command (wrapped by meson to capture output)
[2/4] Compiling C object libcommon.a.p/hw_display_ybx_ssd1677.c.o
[3/4] Compiling C object libqemu-xtensa-softmmu.a.p/hw_xtensa_esp32s3.c.o
[4/4] Linking target qemu-system-xtensa          # zero warnings, exit 0
```

The second build (after the alias addition, §5) was `[1/5] … [3/3] Linking
target qemu-system-xtensa`, also clean. 0061 §5's "not compile-verified" risk
is retired: the model hunks compile and link as written.

## 3. Probe measurements against the rebuilt binary

Launcher (runs inside the builder image because the binary is a Linux/arm64
ELF; nothing shared was written — the report JSON lands in the private copy):

```
$ docker run --rm --platform linux/arm64 \
    -v …/qemu-0069-waveform-apply-20261031:/src \
    -v <lane>/tools/ybx-system-emu:/work:ro \
    -v …/qa-20261029:/art:ro ybx-qemu-builder:20261029 \
    python3 /work/probe_x4pro_epd_waveform_qtest.py \
      --qemu-bin /src/build-ybx/qemu-system-xtensa \
      --rom /art/s3-full-rom-generator-test.bin \
      --report /src/0069-waveform-qtest-report.json
failed: []
default checks: 19 / 19
windows checks: 25 / 25
```

Measured values (all `value == expected` in the JSON at
`qemu-0069-waveform-apply-20261031/0069-waveform-qtest-report.json`; 1176 +
876 qtest commands, virtual clock only):

| check | default phase | windows phase |
|---|---|---|
| power activation release | **350 ms** | **40 ms** (`busy-power-ms=40`) |
| power attributed to category | busy-power-high-ms 350 | busy-power-high-ms **40** |
| pixel waveform release | **350 ms** | **350 ms** |
| waveform attributed | busy-waveform-high-ms 350 | 350 after one paint |
| deep sleep entered (`0x10`) | deep-sleep=1, sleep-entries=1 | same |
| post-sleep paint | frames unchanged (1), busy-rise-edges unchanged (3), sleep-ignored-bytes ≥ 8 | same |
| SW_RESET `0x12` while asleep | deep-sleep stays 1 | stays 1 |
| hardware RESET wake | deep-sleep → 0 | deep-sleep → 0 |
| BUSY trailing after RES rises | **0 (silent, default=0 preserved)** | **1**, released at **10 ms**, busy-reset-high-ms **10** |
| paint after wake | frames 2 | frames 2 |
| categories vs pooled | Σ ≤ busy-high-ms | Σ = 40+700+10 = 750 vs pooled 750 |
| edge balance | rise==fall == **4** | rise==fall == **5** |
| idle BUSY before any command | 0 | **1** (startup effect, §4b) |

Baseline honesty — the same probe against the accepted (pre-0061) binary
`/qemu/build-ybx/qemu-system-xtensa` in the shared tree (read-only mount):

```
failed: ['default/0061_aliases', 'windows/0061_aliases']
default marker: absent ('deep-sleep: {… "Property 'xtensa.esp32s3.ybx-epd-deep-sleep' …}')
windows marker: absent ([Errno 104] Connection reset by peer)
exit=1
```

## 4. What the compiled run exposed (and what was fixed)

**(a) `-global ybx.ssd1677.<prop>` never applied — silent.** The first
windows-phase run measured `power_release_ms_windows=None`,
`power_attributed=0`, `reset_trails_busy=0` — i.e. every override was absent.
Root cause: `system/qdev-monitor.c:1188`
`sscanf(str, "%63[^.=].%63[^=]%n", driver, property, &offset)` cuts the
driver at the first dot, so `ybx.ssd1677` arrives as driver `ybx` and
`qdev_prop_check_globals` (`hw/core/qdev-properties.c:907`) only emits
`warning: global ybx.ssd1677.busy-power-ms has invalid class name` on stderr.
Fix: the probe now emits the structured form
`-global driver=ybx.ssd1677,property=busy-power-ms,value=40`
(`global_window_args()`, probe_x4pro_epd_waveform_qtest.py:194-208), and the
patched device exposes read aliases (§5) so the probe asserts the values the
device actually holds: `power_window_applied=40`,
`waveform_window_applied=350`, `reset_window_applied=10` — measured, pass.
This trap applies to the *shipped* 0037 properties too
(`busy-waveform-ms`/`busy-power-ms`): any browser/launch line using the short
form has been silently running at the 350/350 defaults.

**(b) Startup release is a real reset event.** With `busy-on-reset-ms>0` the
idle check measured `(busy, deep-sleep) = (1, 0)`: the guest-side GPIO model
drives RESET low at line connect, and the board-init write releasing it is
indistinguishable from `EpdBus::reset()` — so the opt-in window arms once at
startup. Physically defensible (a real panel does a boot-load waveform after
reset release), so the probe now asserts it explicitly
(`startup_release_trails_busy=1`) instead of pretending it is silent.
Consequence under the last-writer rule: that startup interval merges into the
next activation's category — measured: startup contributed **0 ms** to
`busy-reset-high-ms` and the first power window still released at exactly
40 ms. At `busy-on-reset-ms=0` (the default, what the live emulator would
ship) nothing changes: `idle=(0,0)` measured in the default phase.

**(c) Host mirror diverged from C on the fall edge.**
`hw/display/ybx_ssd1677.c:24-29` records `busy_fall_edges++` *before* the
`now > busy_started_ms` duration guard, so a window cancelled inside the same
virtual millisecond (the probe's power-off → RESET-low sequence) still counts
an edge. The pre-0069 `Panel.set_busy` mirror nested both under the guard;
that would have failed the compiled `edges_balanced` measurements (4/4
default, 5/5 windows). Fixed in the mirror and pinned by
`test_fall_edge_is_recorded_even_without_elapsed_milliseconds`.

**(d) Probe crashed in two places that the orchestrator would have hit.**
`main()` crashed with `TypeError: 'int' object is not subscriptable` on the
non-check `qtest_commands` entry (both phases, before any report was written),
and against a pre-0061 binary the windows launch died on
`ConnectionResetError` (QEMU exits when `-global` names the absent
`busy-on-reset-ms` property) instead of reporting the alias absence. Both
fixed: `isinstance` filtering in the summary, broad per-phase catch that
still records the value plus `qemu_stderr_tail` in the JSON.

## 5. The v2 patch — `experiments/0069-ssd1677-waveform-sleep-v2.patch`

Sections 1 and 2 (`hw/display/ybx_ssd1677.c`,
`include/hw/display/ybx_ssd1677.h`) are **byte-identical to 0061** — the
compiled run found nothing to change in the model logic. The only delta is
`hw/xtensa/esp32s3.c`: alongside 0061's seven counter aliases, v2 also
aliases the three window *configuration* properties
(`ybx-epd-busy-power-ms` at esp32s3.c:870, `ybx-epd-busy-waveform-ms`,
`ybx-epd-busy-on-reset-ms`) so a caller can verify `-global` actually applied
instead of grepping stderr for §4a's warning. Defaults are untouched:
350/350/0, so the shipped browser behaviour does not change by installing the
patch (the only default-visible behaviour change is 0061's intentional
deep-sleep gate).

Mission-mandated validation, read-only against the pinned checkout:

```
$ git -C ~/.local/state/ybx-emulator-upstream/qemu-0043-agent-audit-20261030 \
      apply --check tools/ybx-system-emu/experiments/0069-ssd1677-waveform-sleep-v2.patch
Checking patch hw/display/ybx_ssd1677.c...
Checking patch include/hw/display/ybx_ssd1677.h...
Checking patch hw/xtensa/esp32s3.c...            # exit 0, shared tree untouched
```

Round-trip proof that the patch reproduces exactly the tree that was built
and measured: apply v2 in a scratch dir seeded with the three files from the
*pristine audit working state*, then `cmp` against the measured copy:

```
$ git apply -v …/0069-ssd1677-waveform-sleep-v2.patch     # in /tmp/0069-roundtrip
Applied patch … cleanly.   (×3)
$ cmp roundtrip/<f> qemu-0069-waveform-apply-20261031/<f>   # all three files
IDENTICAL hw/display/ybx_ssd1677.c
IDENTICAL hw/xtensa/esp32s3.c
IDENTICAL include/hw/display/ybx_ssd1677.h
```

## 6. Host tests — `test_x4pro_epd_waveform.py`

15 tests (`python3 -m unittest test_x4pro_epd_waveform -v` → `Ran 15 tests in
0.078s OK`): the 12 batch-3 cases (unchanged expectations, still passing)
plus `CompiledBinaryMeasurements`:

* `test_fall_edge_is_recorded_even_without_elapsed_milliseconds` — §4c.
* `test_windows_phase_counters_replicate_the_compiled_run` — replays the
  exact windows-phase sequence on the mirror and asserts the compiled
  numbers: power 40 (startup interval absorbed), waveform 700 (2 paints),
  reset 10 (wake reset only), pooled 750, edges 5/5, frames 2,
  sleep-ignored-bytes 10 (mode + 8 paint bytes + SW_RESET byte).
* `test_probe_window_overrides_use_the_structured_global_form` — pins the
  §4a CLI contract so nobody "fixes" the probe back to the truncated form.

No test asserts a duration that the rebuilt binary did not measure; the
measured durations are the *configured* ones (see §7 — that is the point).

Full-suite gate (host-only, no Docker, real output):

```
$ python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q
Ran 506 tests in 28.960s
OK (skipped=11)
```

(the lane's own 15 tests run in 0.078 s; the suite wall-time is the accumulated
batch-1..4 host mirrors, no test in this lane launches QEMU, needs the
network or touches Docker).

## 7. Proven / unproven / shipping risk

Proven now (compiled, this lane):
- The model builds and links unchanged from 0061's proposal.
- Power vs waveform vs reset BUSY attribution partitions the pooled 0037
  counter exactly (Σ=750 = pooled, per-category 40/350/10 on demand).
- Deep sleep parks the interface: every byte after `0x10` is rejected
  (`sleep-ignored-bytes` counted), frames/BUSY edges unchanged, only a
  hardware RESET wakes; SW_RESET does not.
- With `busy-on-reset-ms=0` the accepted behaviour is byte-for-byte
  preserved (default phase green, idle silent).
- `-global` short form is broken for dotted type names — a CLI fact about
  this QEMU, relevant to *every* ybx device property, not just 0069.
- Startup reset-release asserts BUSY when the opt-in window is enabled.

Still NOT proven (do not cite this lane for these):
- **Physical e-paper waveform durations.** The model released after 40/350/10
  ms because the probe *set* 40/350/10. Zero milliseconds of this evidence
  came from a panel. `busy-power-ms=350` remains a placeholder; the real
  booster ramp is unknown; `busy-on-reset-ms` stays default 0 because the SDK
  comment (Ssd1677Driver.cpp:381-390) proves trailing BUSY *may* exist, not
  how long it lasts.
- Real panel sleep semantics (mode byte meaning, CS-timing of the 0x10
  latch) — unchanged open questions from 0061 §4.
- Guest-level behaviour: nothing booted. Under `-accel qtest` the CPU is
  inert, so the factory flash image plays no role in these numbers; whether
  FreeInk's sleep/resume paths still produce the accepted Home frame SHA
  under the new deep-sleep gate must be measured in the live container by the
  orchestrator after deciding to ship.
- Frame SHA stability: only if a lane starts painting between `0x10` and a
  hardware RESET does behaviour change, and that is the regression the gate
  exists to expose.

Risks:
- Shipping a synthetic duration: any `-global ybx.ssd1677.*` window value
  becomes true by construction for everything measured afterwards; the
  per-category counters make the provenance visible but not real. Keep
  350/350/0 until an oscilloscope/panel trace exists.
- The §4a trap cuts both ways: the *fixed* probe now genuinely overrides
  windows; if the orchestrator ships the patch but keeps launching the live
  browser with the short form, it silently keeps 350/350/0 — which is today's
  accepted behaviour, so it is safe, but the aliases now make it verifiable.
- The deep-sleep gate is default-on and is the one behaviour change that can
  alter guest output (frames dropped when painting after 0x10 without reset).
  Verify against the live Home frame before merging the binary.
