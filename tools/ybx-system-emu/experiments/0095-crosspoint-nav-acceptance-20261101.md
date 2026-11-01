# 0095 — CrossPoint navigation acceptance runner, landed live (2026-11-01)

Date: 2026-11-01
Lane: batch-6 `nav-acceptance`
Worktree: `.local/state/xteink-agent-workspaces/ybx-emu-batch6-20261101/nav-acceptance`

Task: run the committed acceptance runner (`run_crosspoint_navigation.py`,
the HTTP evolution of the 0082 lane tool) against a **private** bridge
instance, capture the verdict JSON + per-step PNGs + frame SHAs into the
artifacts tree, prove the precondition and target-SHA gates actually bite,
and record any step where the panel did not move.

## Result

The six-step `home-tour` plan **passes on the live kernel** end to end. Every
settled frame SHA matched the 0082-measured plan, every captured PNG
re-hashes to the state frame, and both negative runs failed exactly as the
gates specify.

- Run A (tour): `navigation_sequence_verified: true`, 6/6 steps, exit 0.
- Run B (tour again, no reboot): aborted `precondition_frame_sha256_mismatch`,
  0 taps, exit 1.
- Run C (one expected SHA corrupted in a scratch copy of the runner): 5/6
  steps pass, step 6 fails `target_frame_sha256_mismatch`, exit 1.
- **No step left the panel unmoved.** Every tap advanced the frame counter and
  changed the frame SHA; no emulator defect found in this run.

## Environment

| Item | Value |
| --- | --- |
| Container | `ybx-x4pro-browser-8795` (private, disposable; stopped at the end) |
| Bind | `192.168.10.239:8795` (this Mac's LAN IP; see the note below) |
| Image | `ybx-qemu-builder:20261029` (`--platform linux/arm64`, set by the launcher) |
| QEMU | live tree `qemu-wf-radio-20261031`, binary SHA-256 `f65db0a7e6940a6c08ea2ffe4e8653227e8fbea70b25f4c695fc7a2de6a68236` (re-hashed from the tree before start; read-only mount) |
| Firmware | `crosspoint-2.0.0rc-x4pro-flash-16m.bin`, SHA-256 `2be70081d645ab1df57e28319f010557e243ef4e3eb06190731f56f91f981bbe` (private copy re-hashed; the runner's firmware gate re-confirmed it over `/api/state`) |
| ROM | `s3-full-rom-generator-test.bin`, SHA-256 `5b950be93a30610322a3b46049e4095cdda04f33f09fa13a9a9a6167b5994bd9` |
| SD | `ybx-test-sd-fat16-mbr-pristine.img` (private copy) |
| Private dir | `~/.local/state/ybx-nav-acceptance-20261101/` (qa copy + firmware/ + roms/ + scratch runner) |
| Artifacts | `~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/nav-acceptance-20261101/` |

The shared container `ybx-x4pro-browser-8784` was never contacted; the runner
has no default URL, the base URL pointed only at `:8795`, and `docker stop`
named only my container.

### About the bind

The brief said "bind 0.0.0.0". The launcher refuses an unspecified host bind
(`start_x4pro_browser.sh:23-28` only allows loopback or private IPv4), and I
do not edit that file. The container-side server does listen on `0.0.0.0`
(`--host 0.0.0.0`, `start_x4pro_browser.sh:87`); the host-side Docker publish
was bound to this Mac's private LAN IP `192.168.10.239` instead — the same
style as the owner's live `192.168.10.239:8784` publish. That makes the
bridge reachable at `http://192.168.10.239:8795` for the LAN-IP base URL the
task required, and equally satisfies "never the loopback-only default".

## Exact commands

```sh
# 0. private media (host):
mkdir -p ~/.local/state/ybx-nav-acceptance-20261101/{qa-20261029,firmware,roms}
cp -c ~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/qa-20261029/{s3-full-rom-generator-test.bin,s3-flash-complete-rom-test.bin,ybx-test-sd-fat16-mbr-pristine.img} \
      ~/.local/state/ybx-nav-acceptance-20261101/qa-20261029/
cp -c ~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/firmware/crosspoint-2.0.0rc-x4pro-flash-16m.bin \
      ~/.local/state/ybx-nav-acceptance-20261101/firmware/

# 1. start my own bridge (from tools/ybx-system-emu):
YBX_QEMU_ROOT=$HOME/.local/state/ybx-emulator-upstream/qemu-wf-radio-20261031 \
YBX_BROWSER_PORT=8795 YBX_BROWSER_BIND_ADDRESS=192.168.10.239 \
YBX_QA_DIR=$HOME/.local/state/ybx-nav-acceptance-20261101/qa-20261029 \
bash start_x4pro_browser.sh

# 2. run A — acceptance tour (firmware switch + reboot-first):
python3 run_crosspoint_navigation.py \
  --base-url http://192.168.10.239:8795 \
  --select-firmware crosspoint-2.0.0rc-x4pro-flash-16m.bin --reboot-first \
  --out ~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/nav-acceptance-20261101/verdict-home-tour.json

# 3. run B — same plan, no reboot (precondition must bite):
python3 run_crosspoint_navigation.py \
  --base-url http://192.168.10.239:8795 \
  --out ~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/nav-acceptance-20261101/refusal-precondition/verdict.json

# 4. run C — scratch copy, one expected SHA corrupted (…b -> …c):
mkdir -p ~/.local/state/ybx-nav-acceptance-20261101/scratch-runner
cp run_crosspoint_navigation.py ~/.local/state/ybx-nav-acceptance-20261101/scratch-runner/run_crosspoint_navigation.py
sed -i '' 's/703980c39d79a8f2dc39242895e01e628b620663ff94d26ace3d1e7ca119d70b/703980c39d79a8f2dc39242895e01e628b620663ff94d26ace3d1e7ca119d70c/' \
    ~/.local/state/ybx-nav-acceptance-20261101/scratch-runner/run_crosspoint_navigation.py
PYTHONPATH=$(pwd) python3 ~/.local/state/ybx-nav-acceptance-20261101/scratch-runner/run_crosspoint_navigation.py \
  --base-url http://192.168.10.239:8795 --reboot-first \
  --out ~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/nav-acceptance-20261101/corrupted-sha/verdict.json

# 5. stop my instance:
docker stop ybx-x4pro-browser-8795

# 6. mandatory suite (green):
python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q
# Ran 832 tests in 34.485s — OK (skipped=10)
```

`diff` between the committed runner and the scratch copy shows exactly one
line changed (the `HOME_SETTINGS_SELECTED` constant), so run C isolates the
target gate.

## Run A — accepted tour

Console verdict:

```json
{
 "channel": "browser_http",
 "sequence": "home-tour",
 "navigation_sequence_verified": true,
 "steps_passed": 6,
 "steps_planned": 6,
 "taps_injected": 6,
 "first_failing_step": null,
 "aborted": null,
 "strict_order_ok": true,
 "reached_screen": "home-settings-selected"
}
```

`exit=0`. Full verdict JSON:
`artifacts/…/nav-acceptance-20261101/verdict-home-tour.json`; firmware gate
`matched` (observed `2be70081…1bbe` == expected). Per-step evidence PNGs
(before/after, 12 files) and `SHA256SUMS.txt` (PNG byte SHAs + the frame SHA
re-hashed from each served PNG) sit next to it.

Observed vs planned SHAs (run A `frame_sha256_chain` vs the plan):

| Step | Tap | SHA before → after | Planned after | Gate | frames | png_matches_state |
| --- | --- | --- | --- | --- | --- | --- |
| open-browse-files | (240,346) | `78e74345…` → `24a14c72…` | `24a14c72…` | matched | 2 → 4 | true |
| back-from-browse-files | (36,60) | `24a14c72…` → `78e74345…` | `78e74345…` | matched | 4 → 6 | true |
| open-library | (240,418) | `78e74345…` → `f2b8f94d…` | `f2b8f94d…` | matched | 6 → 9 | true |
| back-from-library | (36,60) | `f2b8f94d…` → `4b3ab700…` | `4b3ab700…` | matched | 9 → 11 | true |
| open-settings | (240,562) | `4b3ab700…` → `148be125…` | `148be125…` | matched | 11 → 13 | true |
| back-from-settings | (36,60) | `148be125…` → `703980c3…` | `703980c3…` | matched | 13 → 15 | true |

All six observed frames equal the 0082-measured values, `strict_order_ok:
true`, every `png_matches_state: true` (the served PNGs agree with
`/api/state` on all six steps). No step failed, so **no panel-stuck defect**:
each tap moved the panel (frame counter advanced and the SHA changed on every
step).

One live difference from 0082, worth recording: this fresh container painted
**more intermediate refreshes** than the 0082 run (frame deltas 2/2/3/2/2/2
here vs 1/2/2/2/2/2 there), and the Library index popup `5511a830…` appeared
as an intermediate during `open-library` even though the container was fresh
and the guest rebooted — 0082 saw that popup only in a manual first-ever
probe. The runner's permitted-transient handling (`observed_transients`)
absorbed it and the settle gate landed on the planned `f2b8f94d…` Library
frame. Intermediate frames observed live: `4b3ab700…`, `5511a830…` during
open-library and `703980c3…` during open-settings. Behaviour of the gate is
correct; the popup timing is more eager than 0082's note suggested.

## Run B — precondition gate bites

Same plan run immediately after the tour (guest parked on Home with Settings
focused, no reboot):

```json
{
 "channel": "browser_http",
 "sequence": "home-tour",
 "navigation_sequence_verified": false,
 "steps_passed": 0,
 "steps_planned": 6,
 "taps_injected": 0,
 "first_failing_step": null,
 "aborted": "precondition_frame_sha256_mismatch",
 "strict_order_ok": false,
 "reached_screen": null
}
```

`exit=1`, **zero taps** — no input reached the guest before the starting-frame
precondition passed. Full JSON in `nav-acceptance-20261101/refusal-precondition/verdict.json`.

## Run C — corrupted expected SHA fails the step as designed

Scratch copy of the runner with exactly one hex digit corrupted in the
expected landing SHA of the last step (`703980c3…b` → `703980c3…c`), run with
`--reboot-first` so the tour can reach step 6:

```json
{
 "channel": "browser_http",
 "sequence": "home-tour",
 "navigation_sequence_verified": false,
 "steps_passed": 5,
 "steps_planned": 6,
 "taps_injected": 6,
 "first_failing_step": "back-from-settings",
 "aborted": "step_failed:back-from-settings",
 "strict_order_ok": false,
 "reached_screen": "unknown"
}
```

`exit=1`. The failing step's gate record:

```json
{
 "step": "back-from-settings",
 "frame_sha256_before": "148be125d47d446775b487c0a75dd26acc94291f7ad5d1a4366ca6f98cc77636",
 "frame_sha256_after": "703980c39d79a8f2dc39242895e01e628b620663ff94d26ace3d1e7ca119d70b",
 "expected_frame_sha256_after": "703980c39d79a8f2dc39242895e01e628b620663ff94d26ace3d1e7ca119d70c",
 "failure_codes": ["target_frame_sha256_mismatch", "target_frame_not_reached_within_timeout"],
 "png_matches_state": true,
 "target_sha256_gate": "mismatched"
}
```

Note what this proves: the **emulator moved exactly to the real frame**
(`703980c3…b`, PNG evidence agrees — `png_matches_state: true`); only the
plan's corrupted expectation failed. The gate catches plan corruption, not
panel drift, and the strict order check additionally reports
`step_5_reached_unplanned_frame`. Full JSON in
`nav-acceptance-20261101/corrupted-sha/verdict.json`.

## Gates demonstrated live

| Gate | Shown by | Outcome |
| --- | --- | --- |
| Firmware identity precondition | run A `/api/state` firmware sha | `matched`, `2be70081…` |
| Starting-frame precondition | run B | abort, 0 taps, exit 1 |
| Target frame SHA per step | run C | step 6 fails, exit 1 |
| Panel-changed is unconditional | run A frame deltas 2..3 | every step advanced |
| PNG ↔ state agreement | run A + run C | `png_matches_state` true everywhere, including the failing step |
| Strict order / chaining | run A `strict_order_ok`, run C `step_5_reached_unplanned_frame` | as designed |
| No-default URL / loopback guard | run A used an explicit LAN URL; no `--allow-loopback` was needed or passed | 8784 never touched |

## Limits (unchanged from 0082)

`full_acceptance` remains `false` in every verdict: the browser bridge exposes
no GT911 I2C point-read/status-clear counters, so this proves the guest
repainted in reaction to the injected contact, not that the guest read the
point over I2C. Home as a gesture (bottom-edge up swipe) is unreachable over
`/api/tap`; Back is proven via the header arrow. File Transfer was not
exercised. The Library frame still depends on the pristine SD card's contents.

## Host suite

```text
Ran 832 tests in 34.485s

OK (skipped=10)
```

`exit=0` (`python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q`).
The 826-test baseline in the batch brief predates later merged lanes; the
suite is green as required.

## Cleanup

`docker stop ybx-x4pro-browser-8795` — my container only; `docker ps`
afterwards shows `ybx-x4pro-browser-8784` still `Up` and untouched. Private
media and the scratch runner remain under
`~/.local/state/ybx-nav-acceptance-20261101/`; nothing was written into the
live QEMU tree (mounted `:ro`) or the owner's artifacts tree (only read for
copying; my copies were re-hashed).
