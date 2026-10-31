# 0062 — batch 3: execute-Ready navigation acceptance chain with corrected taps

Status: **tooling corrected and covered by host tests; no guest was driven from
this lane.** The lane that owns the live emulator must run the chain (commands
below); nothing here claims a live navigation result that was not measured.

Scope: `x4pro_guest_navigation.py`, `run_x4pro_guest_navigation.py`,
`test_x4pro_guest_navigation.py` (all owned by this lane) plus this report. No
README/QEMU_BRINGUP/experiments index edits, no QEMU rebuild, no Docker, no
HTTP against the orchestrator's container `ybx-x4pro-browser-8784`.

## 1. What was wrong

`experiments/0048-batch2-gui-navigation.md:52-60` shipped the plan with two
coordinates that had never been driven: `settings-system-settings = (240,681)`
and `system-settings-back = (66,63)` ("measured ink bbox" was quoted for both,
but the points were row/glyph centres, not taps that produced a frame change).
`0052-six-agent-verification-pass-20261030.md:28` executed the chain against
the instrumented instance and recorded the working taps:

```
(350,760) (240,453) (210,679) (35,61)
```

`x4pro_guest_navigation.py:212` and `:219` now carry `(210, 679)` and
`(35, 61)`; `:194-195` records that these replace the 0048 guesses. Asserted
by `test_x4pro_guest_navigation.py:75-83` (values) and `:162-163` (plan order);
the region containment is re-checked against the genuine frames at `:784-799`.

A measurement worth recording: neither corrected tap lands on a dark pixel.
`(210,679)` sits in an inter-glyph gap of the "System settings" label and
`(35,61)` sits inside the chevron's stroke gaps of the header arrow —
`is_dark` is false at both points in the genuine frames. A GT911 contact is
routed by row/header geometry, not by ink, so the genuine-frame tests assert
containment in the measured regions instead of ink coverage
(`test_x4pro_guest_navigation.py:784-799`, `:807-810`)
:

| Region (genuine frame) | measured bbox (inclusive) | corrected tap |
| --- | --- | --- |
| "System settings" row text (frame `5d19930c`) | x16..443, y670..692 | (210, 679) inside |
| header back arrow (frame `a8c871b0`) | x24..121, y52..73 | (35, 61) inside |
| header title text (same frame) | starts at x=123 | tap is 88 px clear of it |

## 2. The acceptance plan (deliverable 1)

`x4pro_guest_navigation.py`:

- `ACCEPTED_SCREEN_SHAS` (`:184-192`) — the four accepted SSD1677 landscape
  frame SHAs keyed by screen:
  home `e79c2ad5f2b60b16fe3f20625742d5f1343f5785552d1b32c11f33c42a874672`,
  more-dialog `8b81cec46b2dd5690cc2748d80d01a7ee86ca7ffbb2d57c7a92bcb68354222cf`,
  ybx-settings `5d19930cf416a60c755aec341aaa2749d994998d241def31cbc36a2a9b6b9ba2`,
  system-settings `a8c871b0ea932bd80018b9d8a2bc1efcbf5562ebe173fff3d7732f5377fe0585`.
- `SEQUENCES["home-more-settings-system-settings-back"]` (`:239-259`) —
  Home → More → YBX Settings → System settings → Back, each step with
  `expect_before`/`expect_after` screen predicates evaluated on real panel
  pixels; `DEFAULT_SEQUENCE` (`:261`) makes it the default for both channels.
- `ordered_acceptance_plan()` (`:272-294`) — flattens the sequence into the
  strictly ordered plan: index, raw GT911 point, both predicates and the
  accepted SHA of the expected screen. Requesting `require_accepted_shas=True`
  for a screen nobody has captured raises instead of inventing a value
  (`:291-294`, test `:184-191`).
- `validate_step_order()` (`:297-331`) — strict-order gate: step *k* must
  start on the screen step *k-1* ended on and reach the planned screen; extra
  steps are rejected. Wired into `assess_sequence(..., plan)` as
  `strict_order_ok`/`order_check` (`:404-420`).
- `assess_step()` (`:333-401`) — per-step gates, all of them: injection
  accepted, SSD1677 refresh count increased, visible frame SHA present and
  **changed** (`panel_pixels_unchanged` fires on an identical before/after
  frame even when the SHA gate is relaxed — `:359-361`), guest-ready flag,
  both screen predicates, plus, only for the native channel, GT911 point-read
  +1 and press/release status-clears +2 (`:349-353`).

Gate design decision, stated plainly: by default the accepted SHA is a
*recorded* gate outcome (`accepted_sha_gate`: `matched` / `mismatched` /
`expectation_only` / `missing_readout`, `:371-377`, `:389`) and `--require-accepted-shas`
flips it into a hard failure. A legitimate repaint (selection highlight, clock,
pagination) produces a different but real frame; failing the whole chain on
that would push whoever runs it to loosen something that must stay tight. The
"step must change the panel" rule is *always* a hard failure.

## 3. Runners (deliverable 2)

`run_x4pro_guest_navigation.py` now carries both channels behind one parser:

- Native (`main()` `:415-458`, unchanged behaviour from 0048): launches its own
  patched QEMU in a private temp dir, drives QMP `ybx-touch-*`, reads
  `ybx-epd-*`. Requires `--qemu-bin/--rom/--flash/--sd-image/--out`; without
  them it exits 2 with `native channel requires --…`.
- Browser (`:189-412`): `--base-url` selects it. `HttpTransport` (`:196-241`)
  is the only code that touches a socket (`/api/state`, `/screen.png`,
  `/api/tap`); `browser_state` (`:253-260`) refuses a response missing any of
  `frames/nonwhite/ready/sha256`; `browser_capture` (`:262-289`) decodes the
  **served PNG bytes** through `nav.portrait_from_mono_png` (`x4pro_guest_navigation.py:77-115`)
  — empty or wrong-size bodies raise instead of substituting an expectation —
  writes a per-step PNG and computes the real portrait SHA and screen
  predicate; `run_browser_plan` (`:299-339`) polls until the refresh count and
  the frame SHA move, then assesses and **stops at the first failed step**.
- Refusals: no default URL (`validate_base_url` `:244-251` rejects None, blank
  and non-http(s)); loopback needs `--allow-loopback` as a second deliberate
  choice (`:349-353`) so a mistyped target still cannot fire blindly; an
  unreachable instance writes an error verdict and exits 3, never a pass.
- Honesty: browser-channel steps report
  `guest_gt911_i2c_consumption_observed: false` and `full_acceptance: false`
  (`:402-403`), because that channel has no I2C counters. It also records
  `shared_browser_server_used: true` — the inverse of the native runner's
  `shared_browser_server_used: false` (`:179`).

Verdict example produced by the in-process fake (host test fixture, **not** a
guest measurement — synthetic landscape SHAs, real synthetic pixels), with
`--require-accepted-shas` on:

```json
{"step": "open-more", "raw_xy": [350, 760], "screen_before": "home",
 "screen_after": "more-dialog", "frame_sha256_before": "landscape-00",
 "frame_sha256_after": "landscape-01",
 "expected_frame_sha256": "8b81cec46b2dd5690cc2748d80d01a7ee86ca7ffbb2d57c7a92bcb68354222cf",
 "accepted_sha_gate": "mismatched", "failure_codes": ["accepted_sha256_mismatch"],
 "full_acceptance": false}
```

## 4. Host tests (deliverable 3)

`test_x4pro_guest_navigation.py` — 78 tests (`:71` geometry, `:120` predicates,
`:154` plan/order, `:232` step gates, `:351` sequence verdict, `:421` QEMU
launch contract, `:451` native capture plumbing, `:513-590` `FakeBrowser`
in-process instance, `:584` browser gates, `:694` URL guards, `:735` PNG
round-trip, `:763` genuine-frame tests behind `YBX_GUEST_EVIDENCE_DIR`).

Both directions are covered explicitly:

- accept: `test_full_chain_accepted_step_by_step` (`:592`) — 4 ordered taps at
  exactly `(350,760) (240,453) (210,679) (35,61)`, every step passes, per-step
  PNGs equal the served bytes, exit 0;
  `test_all_gates_confirmed` (`:252`) for the native channel.
- reject SHA unchanged: `test_step_that_does_not_change_the_panel_fails_loudly`
  (`:627`) — frozen panel ⇒ `panel_pixels_unchanged` +
  `epd_refresh_count_not_increased`, chain stops after one tap;
  `test_pixels_unchanged_rejected` (`:272`) and
  `test_unchanged_panel_fails_even_when_gate_is_relaxed` (`:323`) for the
  pure gate.
- reject guest not ready: `test_not_ready_guest_is_refused_before_any_tap`
  (`:652`) — zero taps injected, `failure_codes == ["guest_not_ready"]`;
  `test_not_ready_guest_rejected` (`:334`).
- reject out-of-order chain: `test_wrong_landing_screen_rejects_the_step`
  (`:642`) — `order_check.reason == "step_0_reached_system-settings"`;
  `test_order_check_rejects_a_skipped_screen` / `_a_stale_starting_screen` /
  `_more_steps_than_planned` / `_empty_inputs` (`:193-229`);
  `test_verdict_carries_the_strict_order_check` (`:375`).
- reject wrong accepted SHA: `test_accepted_sha_requirement_rejects_a_different_repaint`
  (`:660`) and `test_required_sha_gate_rejects_a_different_frame` (`:316`);
  `test_sha_gate_mismatch_recorded_without_requiring_it` (`:310`) pins the
  default (record, do not fail).
- reject a fabricated/unreachable frame: `test_unreachable_instance_produces_an_error_verdict`
  (`:671`), `test_rejects_short_frame_readout`,
  `test_rejects_missing_property_readout` (`:486-497`).

Commands run and real output:

```
$ python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q
Ran 324 tests in 16.636s
OK (skipped=10)

$ QA=/Users/yaraslauhancharou/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/qa-20261029 \
  YBX_GUEST_EVIDENCE_DIR=$QA python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q
Ran 324 tests in 14.013s
OK (skipped=1)

$ (cd tools/ybx-system-emu && python3 -m unittest test_x4pro_guest_navigation)
Ran 78 tests in 2.241s
OK (skipped=9)
$ (cd tools/ybx-system-emu && YBX_GUEST_EVIDENCE_DIR=$QA python3 -m unittest test_x4pro_guest_navigation)
Ran 78 tests in 2.160s
OK
```

`skipped=10` without `YBX_GUEST_EVIDENCE_DIR` = the 9 genuine-frame tests plus
one pre-existing skip elsewhere; with the QA tree only a non-navigation test
stays skipped. `git diff --check`: clean. No network is used by any test: the
browser channel is exercised through the `FakeBrowser` transport, and the two
subprocess tests only check CLI refusal paths (exit 2).

## 5. Which numbers are real, and which are expectations

Real guest readouts (this lane verified the bytes for the portrait files):

- Home `e79c2ad5…` and More `8b81cec4…` — 0042 **native QMP** run
  (`probe_x4pro_guest_touch_ui.py` path), with GT911 point-read/status-clear
  counters and refresh 4 → 5.
- YBX Settings `5d19930c…`, System settings `a8c871b0…`, and the Back step
  reproducing `5d19930c…` byte-for-byte — 0043 run of the *instrumented
  browser-channel* guest (`s3-batch2-0043-browser-multistep.json`, frames 7→8→9).
  Measured independently here: the recorded back-from-System-settings PNG is
  pixel-identical to the YBX Settings PNG
  (`test_x4pro_guest_navigation.py:800-805`).
- Taps `(350,760)` and `(240,453)` — driven through native QMP (0042/0043).
- Taps `(210,679)` and `(35,61)` — driven only through the browser
  `/api/tap` channel; never through native QMP with I2C consumption counters.

Expectations / not measured:

- Nothing above is an expectation; what *is* unproven is their **reproducibility
  on the native channel** and on the current QEMU binary `f07744d9…` (chain
  0042+0043+0037+0051, whose 0051 change moved the BUSY counter reset after
  release). No live run was performed in this lane; no SHA here was measured
  in this session.
- Screen predicates and thresholds were re-derived from the genuine frames by
  the conditional tests, not re-derived against 0051 behaviour.
- Wi-Fi: not emulated. The "Connectivity" row that identifies System settings
  is a list label; nothing here proves association, IP, DHCP or TLS.

## 6. What still needs a live run to be called verified

For the orchestrator (this lane must not touch the container):

1. Native full-chain acceptance — the only run that can set
   `full_acceptance: true` and exercise the two corrected taps with I2C
   counters:

   ```sh
   python3 tools/ybx-system-emu/run_x4pro_guest_navigation.py \
     --qemu-bin <patched qemu-system-esp32s3 f07744d9…> \
     --rom <esp32s3_rev0_rom.bin> --flash <factory.bin 206ed966…> \
     --sd-image <sd.img> --out /tmp/nav-0062.json
   ```

   Expected: exit 0, four steps passed, `strict_order_ok: true`, step 1 SHA
   chain `e79c2ad5… → 8b81cec4…`, and `panel_replay_signals` reporting
   `restores_previous_frame: true` on `back-to-settings`. If a step lands on a
   different real frame, re-run with `--require-accepted-shas` before touching
   any threshold, and treat `accepted_sha256_mismatch` as a measurement to
   report, not a threshold to relax.
2. Browser-channel acceptance against a disposable instrumented instance (not
   `ybx-x4pro-browser-8784`):

   ```sh
   python3 tools/ybx-system-emu/run_x4pro_guest_navigation.py \
     --base-url http://<instrumented-host>:<port> --out /tmp/nav-0062-browser.json
   ```

   Verdict must show `navigation_sequence_verified: true` with
   `full_acceptance: false` — that channel cannot claim I2C consumption.
3. Re-record the accepted SHAs only if the panel legitimately differs under the
   0051 binary; until then `ACCEPTED_SCREEN_SHAS` stays as measured.

## 7. Risks and limits

1. Thresholds remain tied to this factory build's theme; a theme/density change
   moves the bands, `screen_state` fails closed to `unknown`, and the
   conditional genuine-frame tests are the tripwire.
2. `HomeActivity.cpp:712` adds a 5th "On Reader" row when the plugin shelf
   directory exists on the SD image; that shifts Settings down one row and
   `(240,453)` would hit "File Transfer" — the screen predicate rejects it
   rather than passing silently.
3. The browser channel reads only what `/screen.png` currently holds; if the
   instance has never painted, `sha256`/PNG checks raise instead of comparing
   against an expectation (`browser_state`, `browser_capture`).
4. Two taps inside one settle window can still coalesce if the render task is
   blocked on `s_epdRefreshDone`; the per-step refresh-count delta catches it.
5. `qemu_command()` is shared with `probe_x4pro_guest_touch_ui.py`; launch-flag
   changes must keep `QemuLaunchContractTests` (`:421-449`) honest.
6. Full-suite wall time is 14–17 s on this Mac — under the 20 s budget, but the
   margin is thin; the settle-loop tests are deliberately capped at
   `settle_seconds=1.0`.
