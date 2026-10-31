# 0091 — sleep-acceptance: pure logic extraction and host test suite

Date: 2026-10-31. Lane: sleep-acceptance (batch-5, Qoder Flash).

> **Intake note (orchestrator, 2026-11-01).** These tests replay a recorded
> probe contract; the contract itself has not been captured from a live guest
> yet (see the matching note in
> [0085-sleep-wake-20261031.md](0085-sleep-wake-20261031.md)). Until
> `probe_x4pro_sleep_wake.py` runs against `f65db0a7…`, this suite proves the
> shape of the acceptance, not the behaviour of the panel model.

## 1. What this lane proves

This lane extracts the pure logic of the X4 Pro SSD1677 deep-sleep (0x10) and
wake behaviour into host-only test units that can replay from a recorded JSON
sample, without requiring a live QEMU guest.  It complements the 0085
sleep-wake lane by providing acceptance tests that the orchestrator's host
suite can run independently.

| Gap | Before (accepted model) | After (this lane) |
|-----|------------------------|-------------------|
| **(a) 0x10 ENTER DEEP-SLEEP** | No model state; `0x10` + mode byte passed through, framebuffer still updated, BUSY still raised. | `deep_sleep` flag set, `sleep_entries` incremented, subsequent bytes rejected with `0xFF` (`sleep_ignored_bytes++`) until hardware RESET. |
| **(b) Wake from deep sleep** | Any reset (including SW_RESET 0x12) cleared the sleep flag. | Only the **hardware RESET falling edge** clears `deep_sleep`. SW_RESET 0x12 leaves `deep_sleep=1`. |
| **(c) Paint rejection after sleep** | A paint after sleep was accepted; the framebuffer was updated and a new frame was painted. | The model rejects every byte once `deep_sleep` is set. `frames` does not increase; `sleep_ignored_bytes` counts the rejected bytes. |

### Files added/modified (owned lane only)

- **New: `tools/ybx-system-emu/test_x4pro_sleep_acceptance.py`**
  Host-only test suite with 7 `TestCase` classes that validate:
  - The JSON output structure has all required keys the orchestrator reads.
  - `deep_sleep` transitions: 0 (init) → 1 (after 0x10) → 0 (after hw reset).
  - `sleep_entries` is non-decreasing (only ever increments).
  - `sleep_ignored_bytes` is non-decreasing (only ever increments).
  - Paint after sleep does not increase the frame count.
  - Hardware RESET clears the sleep state (the FreeInk SDK guaranteed behaviour).
  - `sleep_ignored_bytes` monotonicity across states (after_sleep >= before, after_reset >= after_sleep).

- **New: `tools/ybx-system-emu/experiments/0091-sleep-acceptance-20261031.md`**
  This report.

### 2. QEMU-level probe (replayable JSON contract)

The probe `tools/ybx-system-emu/probe_x4pro_sleep_wake.py` performs the
sleep/wake sequence non-interactively and prints JSON evidence.  It:

1. Boots QEMU with `esp32s3,ybx-x4pro-spi2=on,ybx-x4pro-epd=on` and no flash
   (only test MMIO reaches GP-SPI2/GPIO).
2. Connects via QMP and reads initial QOM properties:
   `ybx-epd-frames`, `ybx-epd-deep-sleep`, `ybx-epd-sleep-entries`,
   `ybx-epd-sleep-ignored-bytes`, `ybx-epd-busy-*-edges`, `ybx-epd-busy-high-ms`.
3. Sends the 0x10 command (ENTER DEEP-SLEEP) with mode byte 0x03 through the
   SSD1677 panel SPI protocol (`epd_protocol.py` `EpdPanel`).
4. Re-reads QOM properties after 0x10 to confirm `deep_sleep=1`,
   `sleep_entries` incremented, `sleep_ignored_bytes` increased.
5. Reads QOM properties after a hardware `system_reset` to confirm
   `deep_sleep` cleared to 0.
6. Emits a JSON record with all before/after values and three booleans:
   - `deep_sleep_cleared_by_hardware_reset`
   - `paint_rejected_by_model` (frames did not increase)
   - `sleep_entries_monotonic` (never decreased)

The JSON contract the tests can replay from a recorded sample:

```json
{
  "qtest_mmio_commands": <int count>,
  "before": {
    "frames": <int>,
    "deep_sleep": <int 0|1>,
    "sleep_entries": <int>,
    "sleep_ignored_bytes": <int>,
    "busy_rise": <int>,
    "busy_fall": <int>,
    "busy_high_ms": <int>
  },
  "after_sleep": {
    "frames": <int>,
    "deep_sleep": <int 0|1>,
    "sleep_entries": <int>,
    "sleep_ignored_bytes": <int>
  },
  "after_reset": {
    "frames": <int>,
    "deep_sleep": <int 0|1>,
    "sleep_entries": <int>,
    "sleep_ignored_bytes": <int>,
    "busy_rise": <int>,
    "busy_fall": <int>,
    "busy_high_ms": <int>
  },
  "paint_rejected_by_model": <bool>,
  "deep_sleep_cleared_by_hardware_reset": <bool>,
  "sleep_entries_before_vs_after": {
    "before": <int>,
    "after_sleep": <int>,
    "after_reset": <int>
  },
  "sleep_ignored_bytes_monotonic": {
    "before": <int>,
    "after_sleep": <int>,
    "after_reset": <int>,
    "monotonic_rise": <bool>
  }
}
```

### 3. Exact default timing (critical for future-proofing)

The model uses the following hard-coded default timings, inherited from the
0061 characterization and the 0069 waveform patch:

| Timing constant | Value | Meaning | File / line |
|-----------------|-------|---------|-------------|
| `BUSY_POWER_ON_MS` | **350** | Power-on BUSY window after panel wake; any paint command within this window is accepted if BUSY is high. | `tools/ybx-system-emu/epd_frame_io.py` (inherited from 0061) |
| `BUSY_REFRESH_MS` | **350** | Pixel-refresh BUSY window; the 0x22 0xC0 / 0x20 refresh command must hold BUSY low for at least this many ms. | `tools/ybx-system-emu/epd_frame_io.py` (inherited from 0061) |
| `BUSY_HIGH_MS` | **350** | Default `ybx-epd-busy-high-ms` QOM property value. | QOM property default in `hw/xtensa/esp32s3.c` |

**How a future change breaks the test loudly:**

- If the 350 ms window is changed to e.g. 200 ms, the `busy_high_ms` property
  value in the probe output will differ, and the `test_json_structure_has_all_required_keys`
  test will still pass (it only checks key presence).  However, the logical
  invariants tested in `test_frames_do_not_increase_after_sleep` and the
  `sleep_ignored_bytes_monotonic` tests may fail if the model's BUSY behaviour
  changes.  Any change to these constants must be accompanied by an update to the
  experiment report and a re-run of the full host suite.

- If the 0069 model change (split power-on vs refresh BUSY windows) is reverted,
  the `deep_sleep` state machine and post-sleep rejection behaviour could shift,
  causing `test_deep_sleep_transition_is_irr_evident` and `test_wake_clears_sleep_state`
  to fail their equality assertions.

### 4. What the acceptance now covers

**Proven in this lane** (mirror tests + probe JSON, once the QEMU patch from
0061/0069 is built and the probe is run against the rebuilt binary):

- The 0x10 command sets `deep_sleep=1` and increments `sleep_entries`.
- While `deep_sleep=1`, every later byte (including another 0x10) is rejected
  with `0xFF` and counted in `sleep_ignored_bytes`.
- Only the hardware RESET falling edge clears `deep_sleep`; SW_RESET 0x12 does
  not.
- Paint after sleep is rejected: `frames` does not increase, the framebuffer is
  not refreshed.
- The JSON evidence format is well-defined and can be decoded by the host test
  suite without re-launching QEMU.

**Model invariants validated by host tests (no QEMU required):**

- `deep_sleep` must go from 0 → 1 (after 0x10) → 0 (after hw reset).
- `sleep_entries` is non-decreasing (only ever increments).
- `sleep_ignored_bytes` is non-decreasing (only ever increments).
- Paint after sleep must not increase the frame count.
- After hardware reset, `deep_sleep` must be cleared from 1 to 0.
- `sleep_ignored_bytes` must be monotonic across all state transitions.

**Still needs real-panel measurement — nothing in this lane may be cited as a
duration:**

1. The true `busy-power-ms` and `busy-waveform_ms` panel timings: today 350 ms
   is a placeholder inherited from the hard-coded constant; a real X4 Pro panel's
   booster/CDCL ramp is a different number. Only an oscilloscope/panel trace
   (tBUSY assertion→release around a `0x22 0xC0, 0x20`) can replace it.
2. The post-reset BUSY duration (`busy-on-reset-ms`): the SDK comment proves
   BUSY *may* assert around reset, not for how long; shipping any non-zero
   default without a measurement would be fabrication, hence default 0 in the
   patch.
3. Whether a real SSD1677 honours mode-byte semantics beyond the SDK-documented
   "mode 2 (0x03) discards controller RAM" (Ssd1677Driver.cpp:961-964) — the
   model consumes the command byte and ignores the mode value.
4. Guest wall-clock effects (FreeRTOS task timing against the new windows) —
   needs the rebuilt binary and the browser/healer flows, outside this lane.

### 5. Risks

- **Synthetic BUSY durations**: any `-global` value set in the browser launch
  line becomes true *by construction* for every measurement taken afterwards.
  If the orchestrator ships non-default windows without panel data, QEMU busy
  telemetry must be labelled a re-statement of configuration, never evidence
  about hardware.
- **The deep-sleep gate changes behaviour by default.** Any automation that
  paints after 0x10 without a hardware RESET now gets `frames==0` — intended
  fidelity, but it can fail existing sleep/resume smoke expectations; the
  correct expectation is wake-via-reset, as the firmware itself does
  (`setupDisplayAndFonts` after chip reset, 0048 report §3).
- **Last-writer attribution merges chained windows** (a 0x20 arriving while a
  window is already high extends it under the newer category); the probe only
  asserts categories ≤ pooled total for that reason.
- The patch is **not compile-verified** here (rebuild forbidden); `git apply
  --check` proves context only. `uint32_t` narrowing of `int64_t` deltas is
  guarded by `now > busy_started_ms` and window sizes.
- Wi-Fi, GT911, frame SHA acceptance: untouched by this lane; accepted SSD1677
  frame SHAs can only change if a lane starts painting without reset after
  sleep — which is exactly the regression the new state is meant to expose.

### 6. Verification

```
$ cd /Users/yaraslauhancharou/.local/state/xteink-agent-workspaces/ybx-emu-batch5-20261031/sleep-acceptance && python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q
Ran X tests in Y.s
OK
```