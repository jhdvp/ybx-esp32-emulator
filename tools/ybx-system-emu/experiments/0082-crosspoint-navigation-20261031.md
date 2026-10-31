# 0082 — CrossPoint 2.0.0rc navigation tap plan and frame-SHA gate

Date: 2026-10-31
Lane: `agents/ybx-emu-b5-crosspoint-nav-20261031`
Worktree: `.local/state/xteink-agent-workspaces/ybx-emu-batch5-20261031/crosspoint-nav`

This lane adds a measured tap plan for stock CrossPoint 2.0.0rc and a runner
that gates every step on the real SSD1677 frame SHA. A step passes only when
the guest was ready, the panel RAM changed, and the settled frame is the
measured target. An unchanged frame is always a failure.

Files added:

- `tools/ybx-system-emu/crosspoint_navigation.py` — plan data, gates, strict
  order check, PNG readout cross-check, browser bridge client and CLI.
- `tools/ybx-system-emu/test_crosspoint_navigation.py` — 34 host tests with an
  in-process fake bridge (no Docker, no network, no guest).

## Result

Live six-step tour on the private container, after `POST /api/reboot`:

```json
{
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

`exit=0`. Firmware gate: expected `2be70081…`, observed `2be70081…`,
`matched`. Every captured PNG re-hashes to the state SHA (`png_matches_state`
true for all six steps).

Refusal run from a Home frame that was not the planned start (guest was on
Home with Settings focused after the tour, no reboot):

```json
{
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

`exit=1`, zero taps: no input was sent before the precondition passed.

## Environment

| Item | Value |
| --- | --- |
| Container | `ybx-x4pro-browser-8794` (private, disposable; the shared 8784 instance was not touched) |
| Bind | `127.0.0.1:8794` |
| Image | `ybx-qemu-builder:20261029` |
| QEMU | `/qemu/build-ybx/qemu-system-xtensa`, QEMU 9.2.2 (`build-ybx/qemu-version.h`), tree `qemu-wf-radio-20261031`, source HEAD `febae182e132e4055529be423a818225ebddaa3a` |
| Firmware | `crosspoint-2.0.0rc-x4pro-flash-16m.bin`, SHA-256 `2be70081d645ab1df57e28319f010557e243ef4e3eb06190731f56f91f981bbe` (`/api/state`) |
| ROM | `s3-full-rom-generator-test.bin`, SHA-256 `5b950be93a30610322a3b46049e4095cdda04f33f09fa13a9a9a6167b5994bd9` |
| SD | `qa-20261029/ybx-test-sd-fat16-mbr-pristine.img` |

The launcher in this worktree is not executable, so it was invoked through
`bash` from `tools/ybx-system-emu`:

```sh
YBX_QEMU_ROOT=$HOME/.local/state/ybx-emulator-upstream/qemu-wf-radio-20261031 \
YBX_BROWSER_PORT=8794 \
bash start_x4pro_browser.sh
```

Live runner:

```sh
python3 tools/ybx-system-emu/crosspoint_navigation.py \
  --base-url http://127.0.0.1:8794 --allow-loopback --reboot-first \
  --out /tmp/cp-nav/live-tour-final.json
```

Refusal:

```sh
python3 tools/ybx-system-emu/crosspoint_navigation.py \
  --base-url http://127.0.0.1:8794 --allow-loopback \
  --out /tmp/cp-nav/live-refusal-final.json
```

## Tap targets

| Target | Raw xy | Derivation |
| --- | --- | --- |
| Browse Files row | (240, 346) | Lyra Home metrics `menuTop = 56 + 242 + 16 = 314`, row pitch `64 + 8 = 72`; row 0 centre 314 + 32 = 346; measured selection band starts at y=314 |
| Library row | (240, 418) | row 1 centre 314 + 72 + 32 = 418; measured label ink y406..433 |
| Settings row | (240, 562) | row 3 centre 314 + 3×72 + 32 = 562; measured label ink y550..577 |
| Header back | (36, 60) | header back is drawn at `band.x + 4, band.y + 4`; measured glyph x24..44, y54..73 on all three pushed screens |

Coordinates are raw GT911 portrait points; the bridge injects them unchanged.
Home rows accept the full row width, so x=240 is the horizontal middle.
Derivation constants are quoted from the firmware in
`crosspoint_navigation.py:8-23` and the target table is
`crosspoint_navigation.py:103-128`.

The fourth Home row (File Transfer, centre ~490) was not exercised by this
lane.

## Measured frames

SHA-256 of the genuine SSD1677 visible RAM, read from `/api/state`:

| SHA-256 | Screen |
| --- | --- |
| `78e743450b8f67ced89eb59d86b7c9da61eb306b4c3e92c31f50b345ad99c296` | Home, Browse Files focused (initial after boot) |
| `24a14c7230b583c7ca042105b7da170061dc819bf61516696ca5d2d5cd7a8a89` | Browse Files (SD card) |
| `4b3ab70080b6027338782995d0e1225183fbfc62fc718869dacef9bed7abbc5a` | Home, Library focused |
| `5511a8302b14fd4a145a1d5dad47dd2c90079bb3110d4cb92387655abc56ade9` | Library, index-build popup (first open only) |
| `f2b8f94d389fa4abe89dc701c522dd9a4010e26d45b866a3816f3ea27f9d8b13` | Library |
| `703980c39d79a8f2dc39242895e01e628b620663ff94d26ace3d1e7ca119d70b` | Home, Settings focused |
| `148be125d47d446775b487c0a75dd26acc94291f7ad5d1a4366ca6f98cc77636` | Settings |

Declared in `crosspoint_navigation.py:76-94`.

Final live chain (`frame_sha256_chain` of the tour JSON), with the refresh
counters from the same run:

| Step | Tap | Frame SHA before → after | frames |
| --- | --- | --- | --- |
| open-browse-files | (240,346) | `78e74345…` → `24a14c72…` | 2 → 3 |
| back-from-browse-files | (36,60) | `24a14c72…` → `78e74345…` | 3 → 5 |
| open-library | (240,418) | `78e74345…` → `f2b8f94d…` | 5 → 7 |
| back-from-library | (36,60) | `f2b8f94d…` → `4b3ab700…` | 7 → 9 |
| open-settings | (240,562) | `4b3ab700…` → `148be125…` | 9 → 11 |
| back-from-settings | (36,60) | `148be125…` → `703980c3…` | 11 → 13 |

Home remembers the focused row, so Back does not return to the initial Home
frame: after Library it lands on `4b3ab700…`, after Settings on `703980c3…`.
Opening a row first repaints Home with the new highlight and then pushes the
activity; those intermediate frames were observed by the runner
(`intermediate_frame_sha256s`): `4b3ab700…` during open-library and
`703980c3…` during open-settings. They are recorded as evidence and are not
gated; the gate is the settled screen SHA.

The Library index popup `5511a830…` was captured once on the first-ever
Library open after the container boot (manual probe, PNG `library.png`
re-hashes to it). It is declared as a permitted transient
(`crosspoint_navigation.py:159`, `:184`, `:195`) but is not required: the
same Library open on a warmed guest, including after `/api/reboot`, settled
directly on `f2b8f94d…`.

PNG cross-check: `served_frame_sha256` (`crosspoint_navigation.py:365-372`)
inverts `epd_frame_io.ssd1677_landscape_to_portrait`, so each served portrait
PNG is hashed as the landscape RAM the bridge hashes. The six evidence PNGs
map back to the settled state SHA:

| PNG | PNG SHA-256 | State match |
| --- | --- | --- |
| `00-open-browse-files.png` | `24a14c72…` | true |
| `01-back-from-browse-files.png` | `78e74345…` | true |
| `02-open-library.png` | `f2b8f94d…` | true |
| `03-back-from-library.png` | `4b3ab700…` | true |
| `04-open-settings.png` | `148be125…` | true |
| `05-back-from-settings.png` | `703980c3…` | true |

This closes the earlier concern that `/api/state` and `/screen.png` could
describe different frames: they agree on all six steps.

## Gates

- `assess_step` (`crosspoint_navigation.py:259-304`) fails with
  `missing_frame_sha256_before/after`, `guest_not_ready`,
  `precondition_frame_sha256_mismatch`, `frames_not_advanced`,
  `panel_frame_unchanged`, or `target_frame_sha256_mismatch`.
  `panel_frame_unchanged` is unconditional: a tap that leaves the panel
  byte-identical is never a success.
- `validate_sequence` (`:307-336`) rejects renamed, reordered, skipped or
  extra steps, unplanned start/landing frames and incomplete runs.
- `--allow-sha-drift` only removes `target_frame_sha256_mismatch` (recorded as
  `target_sha256_gate: drifted`); the changed-frame and ordering rules remain
  hard (`:558-566`).
- The firmware SHA is a precondition (`:512-519`); the measured image is
  `2be70081…` and a different image aborts before any tap unless
  `--allow-firmware-drift` is explicit.
- Preconditions are checked before the loop (`:498-519`), so a wrong starting
  frame or an unready guest produces zero taps.
- Exit codes (`:676-680`): 0 verified, 1 failed/refused, 3 bridge unreachable.

## Host tests

```sh
python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -v -k 'crosspoint'
```

```text
Ran 34 tests in 0.678s

OK
```

Wall clock 1.03 s. Coverage by class in
`crosspoint_navigation.py` tests (`test_crosspoint_navigation.py`):

- plan data, chaining and transients: `PlanDataTests` (`:110-149`)
- PNG readout round-trip: `PngReadoutTests` (`:152-168`)
- gate accept, unchanged, not-ready, wrong start/target, missing readouts:
  `GateTests` (`:171-213`)
- strict order: complete, reordered, skipped, extra, incomplete, unplanned
  landing: `OrderTests` (`:216-255`)
- runner: full chain, unchanged stops after one tap, not-ready and wrong
  start send zero taps, drift, firmware gate, PNG mismatch, reboot wait:
  `RunnerTests` (`:258-356`)
- CLI guards: URL required, loopback opt-in, scheme, unknown sequence:
  `CliGuardTests` (`:359-398`)

The fake bridge is in-process; these tests use no Docker, network or guest.

Full suite (required verification):

```sh
python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q
```

```text
Ran 682 tests in 34.147s

OK (skipped=11)
```

The 34 navigation tests account for ~0.7 s; the ~34 s runtime is the
pre-existing cost of the other 648 tests.

## Limits

- `full_acceptance` is false in every verdict
  (`crosspoint_navigation.py:599-600`). The HTTP bridge exposes no GT911 I2C
  point-read or status-clear counters, so this proves the panel changed and
  the guest reacted, not that the guest read the injected point over I2C.
- Home is not proven as a gesture. CrossPoint maps Home to a bottom-edge up
  swipe and `/api/tap` injects a stationary contact only. Back is proven
  through the header arrow; the Home gesture is unreachable on this channel.
- File Transfer was not exercised.
- The Library frame depends on SD contents and index state; `f2b8f94d…` is
  measured on the pristine FAT16 image above. Another card can settle on a
  different frame, which fails the target gate loudly (`--allow-sha-drift`
  records it, but the changed-frame gate still applies). The index popup is
  first-open state, not a deterministic step outcome.
- Any Home content change (for example header clock/battery glyphs on real
  hardware) changes the Home SHA and fails the target gate by design.
- The disposable 8794 container was stopped after the measurements; the
  shared 8784 instance was not contacted at any point.
