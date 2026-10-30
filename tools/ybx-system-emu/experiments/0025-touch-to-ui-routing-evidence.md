> **CORRECTION — 2026-10-30:** This investigation has progressed and the earlier UI-block diagnosis is superseded by [0036-x4pro-render-notification-evidence.md](0036-x4pro-render-notification-evidence.md). The genuine guest **does** select More: onAction pending=4, selectedAction=4, HomeActivity accepts and activates index 4. onMoreOpen completes plugin-path lookups and requests a redraw. FreeRTOS xTaskGenericNotify returns pdPASS for the render task, yet the task is not observed waking to paint a new SSD1677 frame. Also the supposed +236-byte UiAppHost pointer corruption was a false GDB pre-entry register interpretation: caller HomeActivity=0x3fcec858 and actual YbxHomeUi=0x3fcec944 are two distinct objects. Do not patch Xtensa entry/retw or claim uiReady corruption based on the old 0025 notes. The end-to-end changed-screen acceptance remains false.\n\n# 0025 — Factory X4 Pro GT911 → UI routing, live GDB evidence

Status: **input reaches the YBX UI action callback; EPD navigation remains
UNVERIFIED**. This is an isolated developer-tool experiment, not a
production-firmware fix or an accepted full-device emulator.

## Evidence from real, unchanged factory firmware 092c2be5

Loaded the matching 100 MB symbol-bearing `x4pro/firmware.elf` in Espressif
Xtensa GDB and attached to the **real dual-core ESP32-S3 guest CPU** with
`-gdb tcp:0.0.0.0:25997`. QEMU booted with original factory flash, the
opt-in GT911, BM8563/CW2017, SDMMC, GP-SPI2 and SSD1677 panel. The QMP
touch injector issued a short contact and release at raw GT911 (350,760).

Live GDB checkpoints confirmed:

| Executed production code | Captured fact |
| --- | --- |
| `InputManager::pollGt911()` | `touchReleasedEvent=true`; last-down native `x=760,y=129`; suppressed=false, multi-contact=false |
| `InputManager::wasTouchTap()` | Release flag true, no suppression, no multi-contact, no excessive release slop |
| `MappedInputManager::wasScreenTapped()` | Normalized `nx=0.951188982,ny=0.26931107`; renderer maps logical `(350,760)` |
| `UiAppHost::routeTouch()` | `InputSnapshot.touchReleased=true, touchX=350, touchY=760`; no longPress or swipe |
| `YbxHomeUi::onAction()` | Callback **entered** from the original factory guest |
| `HomeActivity::loop()` | Previously confirmed `YbxHomeUi::selectedAction()` executing periodically |

This disproves earlier hypotheses that I²C press/release is not
classified as a tap, that the orientation mapping misses the selected
button, that FreeRTOS is stopped, or that `UiAppHost` never sees the
release. SYSTIMER comparator0/1 callbacks continuously fired in
separate 0024 evidence.

**Unresolved:** the SSD1677 visible frame SHA remains
`e79c2ad5f2b60b16fe3f20625742d5f1343f5785552d1b32c11f33c42a874672`
and refresh count stays two after tapping the home screen. The
`YbxHomeUi::onAction` argument printed *at function entry* contained
implausible action IDs/values; **do not interpret those numbers as
real event data**, because Xtensa ABI register arguments can be
misreported by DWARF at an optimized prologue.

## New reproducible post-callback checkpoints

Use `0025-ybx-home-action-gdb.gdb`. The script intentionally stops
**after** `self.pending=event.value` rather than at the potentially
misleading callback prologue. It captures:

1. Real `onAction` value written into `pending`.
2. `selectedAction` post-route `pending` and input snapshot.
3. Whether `HomeActivity` receives `touched >= 0`.
4. Whether it advances to `activateSelection()`.

To run: execute the existing opt-in original-guest probe with
`--gdb-port 25997 --debug-hold-seconds 15` after allowing enough
boot time to obtain the YBX home frame. Publish the GDB port only on
`127.0.0.1` for a temporary Docker container, use the matching ELF
from the same factory image, then run:

```sh
xtensa-esp32s3-elf-gdb -batch -nx \
  /path/to/matching/x4pro/firmware.elf \
  -x tools/ybx-system-emu/experiments/0025-ybx-home-action-gdb.gdb
```

The probe's `GDB_READY_FOR_ATTACH` marker is the debugger attach
checkpoint. GDB breakpoints can perturb virtual scheduling, so accept
the UI navigation milestone **only** if, in a subsequent autonomous
non-debug run, real guest input changes the nonwhite SSD1677 visible
frame and increases the actual display-refresh count. Do not close
Kaneo #38 until that independently succeeds.

Prior GDB evidence was captured on the authorized Mac in
`/tmp/ybx-gt911-0025b.log` and `/tmp/ybx-gt911-0026.log`;
these temporary logs were **not** committed. The trace script above
is committed for reproducibility. No production firmware changed.
