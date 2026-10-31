# 0061-batch3-epd-waveform — SSD1677 power/waveform attribution, deep sleep, BUSY-around-reset

Date: 2026-10-31. Lane: epd-waveform (batch 3, six-Qwen orchestration).
Baseline binary under audit: SHA-256 `f07744d9…` (chain 0042+0043+0037+0051),
source `/Users/yaraslauhancharou/.local/state/ybx-emulator-upstream/qemu-0043-agent-audit-20261030`.

Scope executed: host-only. One Python mirror + tests, one proposed QEMU patch
validated **read-only** with `git apply --check` (never applied, never rebuilt),
one qtest probe script for the orchestrator. No Docker, no guest, no shared
emulator touch, no edits to shared files (README, experiments/README.md, the
telemetry scripts) — all four owned files only.

## 1. The three gaps, characterised against the accepted source

All line numbers are the audit checkout as built.

**(a) Power vs waveform is a timing *choice* without attribution.** Patch 0037
already split the window: `epd_transfer` arms
`waveform ? s->busy_waveform_ms : s->busy_power_ms`
(hw/display/ybx_ssd1677.c:184-186) and both properties default to 350
(:294-297), so a "power cost" measured through the running emulator is still
350 ms by construction. Worse, the only BUSY-duration telemetry is the pooled
`busy_high_ms` (:24-29, exposed as `busy-high-ms` :276-277): even with
distinct values set, the measurement cannot attribute a single millisecond to
power vs waveform vs anything else. Recorded in
0048-batch2-epd-lifecycle.md §3 ("Timing fidelity").

**(b) Deep sleep (0x10) has no model state.** `epd_transfer`'s command branch
handles only 0x12 and 0x20 (:164-187); `epd_expected_params` (:116-128) has no
0x10 entry, so the FreeInk sleep pair `0x10` + mode byte `0x03`
(Ssd1677Driver.cpp:965-966, `deepSleep()` :956-966) leaves the model
untouched: a post-sleep `0x24` still writes RAM, `0x20` still paints a frame
and still raises BUSY. QEMU cannot be cited for sleep/resume regressions.

**(c) RESET drives BUSY *low*, real panels trail it.** `epd_reset_gpio` (:55-65)
acts only on the falling edge (`timer_del` + `epd_set_busy(s, false)` +
`epd_clear_ram`); the rising branch is empty. The SDK's fixed settle exists
precisely because real hardware is busy through the post-reset boot-load:
`Ssd1677Driver.cpp:381-390` — *"Active-high BUSY may not have asserted by the
first GPIO sample, so waitBusy() alone is not a substitute for this delay"* →
`delay(10)` after SWRESET; `EpdBus.cpp:202-230` (`reset()` HIGH/LOW/HIGH,
pulse lines :221-226). QEMU therefore cannot catch firmware that skips the
settle.

## 2. Deliverables

### 2.1 Host model + tests — `tools/ybx-system-emu/test_x4pro_epd_waveform.py`

Two mirrors in one file: `Panel` (line-for-line `ybx_ssd1677.c` as accepted,
including 0037 edge telemetry and the 0051 clear-after-release ordering in
`epd_reset` :231-249) and `PatchedPanel` (mirror of the proposed 0061 patch).
Each gap gets a failing-on-the-accepted-behaviour characterisation and a
passing scenario on the patched mirror:

* (a) `test_accepted_defaults_measure_power_cost_as_350_by_construction`,
  `test_accepted_pooled_counter_has_no_attribution_fields` vs
  `test_patched_model_separates_power_waveform_and_reset_windows`.
* (b) `test_accepted_model_paints_right_after_deep_sleep` vs
  `test_patched_sleep_rejects_every_byte_until_hardware_reset`,
  `test_patched_sleep_keeps_the_glass_image_and_the_pending_release`.
* (c) `test_accepted_model_gives_no_signal_for_a_skipped_settle` vs
  `test_patched_reset_window_is_opt_in`,
  `test_patched_reset_trails_busy_and_a_skipped_settle_is_caught`,
  `test_patched_reset_falling_edge_cancels_an_armed_waveform_window`.
* `ProbeContract` pins the byte sequences (`0x22 0x03, 0x20, 0x10 0x03` per
  Ssd1677Driver.cpp:618-622 + :965-966) shared with the probe script, imported
  without launching QEMU.

Modelled decisions that must be read as model choices, not datasheet quotes:
sleep is entered by the **0x10 command byte** (the following mode byte lands in
`sleep-ignored-bytes`, so the counter starts at 1 per sleep); only the
**RESET falling edge** wakes (SW_RESET 0x12 deliberately does not); the glass
image (`visible`) survives sleep, RAM is re-cleared by the wake reset exactly
as the accepted `epd_reset_gpio` already does.

### 2.2 Proposed QEMU patch — `experiments/0061-ssd1677-waveform-sleep.patch`

106 added lines across three files (SHA-256 of the patch file in §3):

* `include/hw/display/ybx_ssd1677.h`: `enum EpdBusyReason` (NONE/POWER/
  WAVEFORM/RESET), `busy_on_reset_ms` (uint16, default 0), `busy_reason`,
  cumulative `busy_power/waveform/reset_high_ms`, `deep_sleep`,
  `sleep_entries`, `sleep_ignored_bytes`.
* `hw/display/ybx_ssd1677.c`:
  – `epd_set_busy` splits the counted high interval into the category that
    armed it (last-writer owns a chained interval; the pooled `busy_high_ms`
    stays byte-for-byte compatible);
  – `epd_reset_gpio` clears `deep_sleep` on the falling edge and, **only when
    `busy_on_reset_ms > 0`**, asserts BUSY on the rising edge for that window;
  – `epd_transfer` rejects every byte once `deep_sleep` (counter
    `sleep_ignored_bytes`), sets the state on command 0x10, and 0x12 re-arms
    the reset window instead of releasing when the window is enabled
    (Ssd1677Driver.cpp:385-388 rationale); 0x20 stamps POWER/WAVEFORM reason
    before arming;
  – `epd_reset` clears the new fields after the release (0051 ordering kept);
  – read-only QOM properties `busy-power-high-ms`, `busy-waveform-high-ms`,
    `busy-reset-high-ms`, `deep-sleep`, `sleep-entries`,
    `sleep-ignored-bytes`; settable `busy-on-reset-ms=0` default.
* `hw/xtensa/esp32s3.c`: matching `ybx-epd-*` aliases inside the existing
  `if (ss->ybx_x4pro_epd)` block, so QMP sees them on `/machine/soc`.

**Default behaviour is unchanged**: with no `-global` overrides the window
defaults (350/350) and `busy-on-reset-ms=0` reproduce the accepted build
(only the new sleep gate changes behaviour, which is the point of gap (b)).
Non-default durations are only ever what the operator sets; nothing in this
patch invents a millisecond.

Validated read-only — never applied, never rebuilt:

```
$ git -C ~/.local/state/ybx-emulator-upstream/qemu-0043-agent-audit-20261030 \
      apply --check -v tools/ybx-system-emu/experiments/0061-ssd1677-waveform-sleep.patch
Checking patch hw/display/ybx_ssd1677.c...
Checking patch include/hw/display/ybx_ssd1677.h...
Checking patch hw/xtensa/esp32s3.c...
exit=0
```

### 2.3 Probe for the orchestrator — `tools/ybx-system-emu/probe_x4pro_epd_waveform_qtest.py`

`--qemu-bin <path> --rom <path> [--report <json>]`. Two QEMU launches of the
**rebuilt** binary, qtest stdio + QMP aliases, `-S`-free but flash-free (only
test MMIO reaches GP-SPI2/GPIO), virtual clock via `clock_step` only:

* phase `default` (no `-global`): asserts the 350 ms release still happens,
  RESET pulses stay silent (backward compatibility), post-sleep painting is
  rejected (`frames`/`busy-rise-edges` unchanged, `sleep-ignored-bytes ≥`
  bytes sent), SW_RESET does not wake, hardware RESET does;
* phase `windows` (`-global ybx.ssd1677.busy-power-ms=40,
  busy-waveform-ms=350, busy-on-reset-ms=10`): power release exactly the
  configured 40 ms counted only into `busy-power-high-ms`, waveform 350 ms
  into `busy-waveform-high-ms`, the trailing BUSY after RES rises released
  after 10 ms counted into `busy-reset-high-ms`, and the three categories
  partition the pooled `busy-high-ms`.

A pre-0061 binary is reported as `0061_aliases: absent`, exit 1. **This lane
did not run the probe** — it needs the rebuilt binary; the orchestrator runs
it after applying the patch, exactly like the 0037/0051 probes before it.

## 3. Commands and real output

```
$ cd ~/.local/state/xteink-agent-workspaces/ybx-emu-batch3-20261031/epd-waveform
$ cd tools/ybx-system-emu && python3 -m unittest test_x4pro_epd_waveform -v
Ran 12 tests in 0.036s
OK
$ python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q
Ran 300 tests in 13.947s
OK (skipped=7)
$ python3 -m py_compile probe_x4pro_epd_waveform_qtest.py
probe-compiles
$ shasum -a 256 experiments/0061-ssd1677-waveform-sleep.patch
6fcf30c0dc32e7247d035a2944a3f775562361b0597d68980a2175844843cc31
```

Full-suite discovery safety: the probe defines no `TestCase` and importing it
only pulls stdlib; the 13.9 s baseline (batch 2 ended at 0.4 s for 177 tests —
the growth is other batch-2/3 files already in-tree, none launched QEMU; the
run above was the whole suite, host-only, no Docker).

## 4. What the model can now prove vs what still needs a real panel

Provable in this lane (mirror tests, once the patch is built + probed):
ordering of reset/sleep/window interactions; that the accepted build paints
after 0x10 and gives no settle signal; that the patched build rejects bytes,
wakes only on hardware RESET, and attributes BUSY time per category; default
byte-for-byte compatibility (350/350, silent reset).

**Still needs real-panel measurement — nothing in this lane may be cited as a
duration:**
1. The true power-only activation time (`busy-power-ms`): today 350 is a
   placeholder inherited from the hard-coded constant; a real X4 Pro panel's
   booster/CDCL ramp is a different number. Only an oscilloscope/panel trace
   (tBUSY assertion→release around a `0x22 0xC0, 0x20`) can replace it.
2. The post-reset BUSY duration (`busy-on-reset-ms`): the SDK comment proves
   BUSY *may* assert around reset, not for how long; shipping any non-zero
   default without a measurement would be fabrication, hence default 0.
3. Whether a real SSD1677 honours mode-byte semantics beyond the SDK-documented
   "mode 2 (0x03) discards controller RAM" (Ssd1677Driver.cpp:961-964) — the
   model consumes the command byte and ignores the mode value.
4. Whether the chip enters sleep on command or on CS deassert; the model has no
   CS timing, the datasheet question is untouched.
5. Guest wall-clock effects (FreeRTOS task timing against the new windows) —
   needs the rebuilt binary and the browser/healer flows, outside this lane.

## 5. Risks

* **Synthetic BUSY durations**: any `-global` value set in the browser launch
  line becomes true *by construction* for every measurement taken afterwards.
  If the orchestrator ships non-default windows without panel data, QEMU busy
  telemetry must be labelled a re-statement of configuration, never evidence
  about hardware; the per-category counters make the fabrication visible but
  do not make it real. Keep `busy-on-reset-ms=0` (and 350/350) until item §4.1
  /§4.2 measurements exist.
* **The deep-sleep gate changes behaviour by default.** Any automation that
  paints after 0x10 without a hardware RESET now gets `frames==0` — intended
  fidelity, but it can fail existing sleep/resume smoke expectations; the
  correct expectation is wake-via-reset, as the firmware itself does
  (`setupDisplayAndFonts` after chip reset, 0048 report §3).
* Last-writer attribution merges chained windows (a 0x20 arriving while a
  window is already high extends it under the newer category); the probe only
  asserts categories ≤ pooled total for that reason.
* The patch is **not compile-verified** here (rebuild forbidden); `git apply
  --check` proves context only. `uint32_t` narrowing of `int64_t` deltas is
  guarded by `now > busy_started_ms` and window sizes.
* Wi-Fi, GT911, frame SHA acceptance: untouched by this lane; accepted SSD1677
  frame SHAs can only change if a lane starts painting without reset after
  sleep — which is exactly the regression the new state is meant to expose.
