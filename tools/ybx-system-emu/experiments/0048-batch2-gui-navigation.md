# 0048 — multi-step GUI navigation acceptance for the genuine X4 Pro guest

Status: **host tooling and host tests complete; the multi-step guest run itself
has NOT been executed in this session.** No navigation claim below is made
without a recorded genuine guest frame.

Scope: extend the accepted single-tap acceptance (Home → More, 0042, commit
`0241de54`) into an ordered multi-step chain with per-step image evidence,
screenshot hashes and machine-readable failure causes. No firmware, QEMU,
shared browser emulator or Docker change.

## What was already proven, and by which channel

| Transition | Proof channel | Frames |
| --- | --- | --- |
| Home → More dialog | native guest, unmodified factory flash, `probe_x4pro_guest_touch_ui.py` | `e79c2ad5f2b60b16…` → `8b81cec46b2dd569…`, 1 GT911 point read, 2 status ACKs, refresh count 4 → 5 (`s3-x4pro-gpio6-pcpu-irq-0042-2s-final.json`) |
| More → YBX Settings | shared browser server `/api/tap` on the telemetry preview instance only | tap `(350,760)` then `(240,453)`, ending at frame 7 `5d19930cf416a60c…` (`s3-batch2-0043-browser-home-more-settings.png` + `-state.json`, recorded in `0050-qwen38-telemetry-integration-20261030.md:70-78`) |
| YBX Settings → System settings → back | shared browser server only | `5d19930cf416a60c…` → `a8c871b0ea932bd8…` → `5d19930cf416a60c…` (`s3-batch2-0043-browser-multistep.json`) |

The browser channel has no per-step GT911 I2C consumption counters, no
per-step SSD1677 refresh delta and no screen predicate, so it cannot satisfy
the acceptance bar this repository set in 0042. Closing that gap is the point
of this experiment.

## Coordinate space

`probe_x4pro_guest_touch_ui.py:81-91` (`x4pro_raw_to_portrait`) shows the X4
Pro panel swap/flip and the renderer portrait rotation cancel, so a raw GT911
contact `(x, y)` lands on portrait pixel `(x, y)` unchanged. Every coordinate
below is therefore both a raw digitizer value and a portrait pixel. A host
test asserts this identity for all targets
(`test_x4pro_guest_navigation.TargetGeometryTests`).

## Geometry measured from genuine guest frames

Measured by decoding the recorded portrait PNGs written by
`epd_frame_io.save_mono_png` (1bpp, MSB-first, cleared bit = ink). Values are
ink pixels or longest contiguous horizontal ink runs inside the band.

| Band (portrait x0,y0,x1,y1) | home | More dialog | YBX Settings | System Settings |
| --- | --- | --- | --- | --- |
| header rule `27,79,454,83` | 854 | 854 | 0 | 0 |
| dialog top run `40,239,440,241` | 12 | 361 | 5 | 0 |
| dialog bottom run `40,558,440,560` | 0 | 354 | 7 | 0 |
| "System settings" row `10,665,450,697` | 0 | 0 | 1978 | 0 |
| "Connectivity" row `65,505,175,536` | 0 | 0 | 61 | 640 |

Separation is 16x or better in every case, which is why the shipped
thresholds sit at half the weakest genuine value
(`x4pro_guest_navigation.py:57-77`).

Derived tap targets (`x4pro_guest_navigation.py:113-146`):

| Target | Raw GT911 | Basis |
| --- | --- | --- |
| `home-more` | `(350,760)` | 0042 native run; measured tab pill x246..452 y741..796 |
| `more-settings` | `(240,453)` | `HomeActivity.cpp:714-716` OPTIONS[2] = `STR_SETTINGS_TITLE` (4-option branch, matching the captured dialog); measured label ink bbox x195..285 y444..466; identical to the point that drove the 0043 browser capture |
| `settings-system-settings` | `(240,681)` | measured row ink bbox x16..443 y670..692 in frame `5d19930c` |
| `system-settings-back` | `(66,63)` | measured header arrow ink bbox x24..108 y54..73 in frame `a8c871b0` |
| `more-dismiss` | `(240,650)` | `OptionPopup.h:97-104` dismisses on a release outside the dialog rect; measured fully white gap y565..730 |

The dialog is a modal overlay, not an activity: `HomeActivity.cpp:376-378`
routes input to `OptionPopup` before the home screen sees it, which is why a
release anywhere outside `OptionPopup.h:245-249`'s centered rect closes it.

## Added tooling

- `x4pro_guest_navigation.py` — pure host logic, no QEMU, no sockets:
  `screen_state()` (image predicate), `assess_step()` (per-tap gates),
  `assess_sequence()` (all-or-nothing verdict), `failure_diagnostics()`
  (ordered machine-readable codes), `panel_replay_signal()` (records whether a
  back step repaints the byte-identical frame; reported, never gated, because
  the genuine 0043 back tap did reproduce `5d19930c` exactly while a
  legitimate repaint may not).
- `run_x4pro_guest_navigation.py` — one QEMU boot, then N ordered taps. Each
  step captures the panel before and after, writes a per-step PNG, and is
  accepted only on: QMP injection accepted, guest I2C point read +1, guest
  status clears +2 (press and release), SSD1677 refresh count increased,
  visible frame SHA changed, expected screen reached and the previous screen's
  signature gone. It polls for the refresh within a bounded settle window
  instead of trusting one fixed sleep.
- `probe_x4pro_guest_touch_ui.py` — `qemu_command()` extracted verbatim from
  `run()` so both scripts launch the same opt-in machine
  (`ybx-x4pro-i2c/gt911/sd-power/spi2/epd=on`); behaviour unchanged.
- `test_x4pro_guest_navigation.py` — 42 host tests.

Per-step evidence, not just an end-to-end SHA, is what makes the chain
strict: a step that "passes" because the *previous* step's repaint landed late
is rejected by the refresh-count and point-read deltas.

## Test results

```
python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py'
Ran 205 tests ... OK (skipped=6)

YBX_GUEST_EVIDENCE_DIR=<qa-20261029> python3 -m unittest discover \
    -s tools/ybx-system-emu -p 'test_*.py'
Ran 205 tests ... OK
```

The 6 conditional tests re-derive every shipped threshold from the four
genuine guest captures and assert each one classifies to the right screen, so
the predicates are validated against real pixels, not only against synthetic
frames built in the test. `git diff --check` clean.

## Not verified here

- No live multi-step guest run was executed in this session: the accepted
  native run needs ~33 s of boot plus a few seconds per step, which exceeds
  the 20 s guest-QEMU budget for this shared Mac. The 0042 and 0043 artifacts
  above are the only real navigation evidence.
- The browser `/api/tap` server, its QEMU process and its Docker containers
  were not touched, restarted or driven.
- ESP32-S3 Wi-Fi is not emulated; the Settings screen reached through
  `more-settings` is the offline YBX Settings list, and its Connectivity row
  proves nothing about a radio.
- Physical e-paper waveform, ghosting and touch pressure are out of scope.
- Screens nobody has captured still classify as `unknown`; that is a deliberate
  dead end for a claim, not a pass.

## Risks

1. Thresholds are tied to this factory build's theme (fonts, row pitch ~61 px,
   dialog at 3/4 width). A theme or density change moves the bands; the
   conditional genuine-frame test is the tripwire, and `screen_state` fails
   closed to `unknown`.
2. `settings-system-settings` and `system-settings-back` coordinates are
   measured from genuine pixels but were never driven through the native
   QMP path with I2C counters; the first live run may need the y adjusted
   within the measured row height (±15 px) if the guest lands on a row gap.
3. The runner assumes the More dialog has 4 options. `HomeActivity.cpp:712`
   adds a 5th "On Reader" row when the YBX plugin shelf directory exists,
   which shifts Settings down one row and would make `more-settings` hit
   "File Transfer"; the step would then fail on the screen predicate rather
   than silently pass.
4. Two taps inside one settle window can coalesce if the render task is still
   blocked on `s_epdRefreshDone`; per-step refresh-count deltas catch that.
5. `qemu_command()` is now shared by two scripts, so a launch-flag change must
   keep `test_x4pro_guest_navigation.QemuLaunchContractTests` honest.

## How a human verifies this

Build nothing new: use the already-patched, already-compiled QEMU at
`ybx-emulator-upstream/qemu-rominit-20261029` and the factory flash/SD image
from the QA tree, in a private shell (never against the browser instance):

```sh
python3 tools/ybx-system-emu/run_x4pro_guest_navigation.py \
  --qemu-bin <patched qemu-system-esp32s3> --rom <esp32s3_rev0_rom.bin> \
  --flash <factory.bin> --sd-image <sd.img> --out /tmp/nav-0048.json
```

Exit code 0 only when every step passed. Inspect
`/tmp/nav-0048-step{N}{a,t}.png` and the `frame_sha256_chain`; a genuine run
should reproduce `e79c2ad5…` → `8b81cec4…` on step 1 and end with the back
step reporting `restores_previous_frame: true`.
