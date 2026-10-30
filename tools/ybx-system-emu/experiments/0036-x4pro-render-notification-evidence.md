# 0036 — X4 Pro More tap reaches FreeRTOS render notification, but render task stays blocked

Date: 2026-10-30. Status: **confirmed guest behavior; root cause still open**. No firmware or default-QEMU fix is claimed.

## Exact experimental environment

The **original, unmodified** X4 Pro 092c2be5 factory firmware, matching symbol-bearing `.pio/build/x4pro/firmware.elf`, runs in the isolated QEMU ESP32-S3 with GP-SPI2 → SSD1677, GPIO-driven GT911, MBR FAT16 SD, BM8563 and CW2017. The guest starts with `-S -gdb tcp:0.0.0.0:<localhost-published-port>`; the matching Espressif `xtensa-esp32s3-elf-gdb` attaches before the CPU starts. Factory flash SHA256 `206ed9661bd71477d1a54397c5a2cfe490179a3f59ab094f1e7421745c609523`. Test touch: raw GT911 (350,760), 150 ms, after a mature home screen with **two real EPD refreshes and 4,743 nonwhite panel bytes**.

The experiment replays the same GT911 press/release to actual guest I2C registers 0x814E/0x8150. The guest reads the coordinates and ACKs the press and release. All GDB inspection is read-only, but can perturb virtual scheduling.

## Verified original-code execution

| Stage | Exact guest-side evidence |
| --- | --- |
| InputManager → MappedInputManager | Real release classified as tap at portrait (350,760). |
| UiAppHost | The snapshot contained `touchReleased=true, touchX=350, touchY=760`. |
| `YbxHomeUi::onAction` | **POST-STORE** `pending=4`, by break at `0x42296480`, after the `s32i` at `0x4229647d`. |
| `YbxHomeUi::selectedAction` | **POST-ROUTE** `pending=4`, at `0x4218255b`. |
| `HomeActivity::loop` | Accepted `touched=4`, reached `activateSelection()` branch at `0x421179eb`. |
| `HomeActivity::onMoreOpen()` | Entered `0x421172c4`; `ybxMore` pointer nonnull. |
| Plugin lookup | Both `PluginLocations::findPluginDir` (`0x421172fa`) and `pluginhttp::catalogDownloadDir` (`0x42117304`) returned. **Not blocking**. |
| Menu request | Reached `Activity::requestUpdate` call (`0x4211734d`) and its return (`0x42117355`). |
| ActivityManager update | Reached `xTaskGenericNotify` call (`0x4210225d`) for **renderTaskHandle=0x3fcec420**. |
| FreeRTOS notification | `xTaskGenericNotify` returned **0x1 (pdPASS)** at `0x42102260`. |
| Render task | No post-notification `ulTaskGenericNotifyTake` return at `0x421018f4`, no subsequent `HomeActivity::render`, no additional SSD1677 SPI display frames within bounded ~8–15 s guest probes. |

The **stricter acceptance gate remains false**: `true_guest_touch_to_ui_verified=false`, real panel frame unchanged (SHA256 `e79c2ad5f2b60b16fe3f20625742d5f1343f5785552d1b32c11f33c42a874672`). **Application action execution is verified, visually updated UI is NOT.**

## Correction of an earlier GDB/DWARF misdiagnosis (important)

Prior 0025 notes incorrectly stated that `UiAppHost::uiReady` was stored 236 bytes too far due to a QEMU Xtensa ABI bug. **That conclusion is disproven**. The pre-`entry` GDB register `a2=0x3fcec858` belongs to the caller, **HomeActivity**, not YbxHomeUi. The caller's assembly loads its `ybxHomeUi` unique_ptr from `HomeActivity+116` into outgoing `a10=0x3fcec944` immediately before `callx8 renderUi` (HomeActivity::render at `0x42118ad4..0x42118ae9`). Xtensa `entry` rotates `WINDOWBASE=0xD→0xF`, revealing the legitimate callee `a2=0x3fcec944`; after `deviceContext`, the window stays 0xF and `a2` remains 0x3fcec944. `UiAppHost` begins at offset 0 of its actual `YbxHomeUi` instance, and `uiReady` is offset +992 from **that** object: `0x3fcec944+992=0x3fcecd24`, exactly where the compiled store writes 1. The earlier read of `0x3fcecc38` used the **HomeActivity pointer** as the YbxHomeUi base and is NOT a valid `uiReady` diagnostic. Do not introduce an Xtensa `entry`/`retw` patch based on this false alarm. The original case for a QEMU CPU register corruption has been **withdrawn**.

## On-device evidence and reproducibility

Evidence lives on the authorized Mac under
`~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/qa-20261029/diagnostics-20261030/`, with checksum manifest `SHA256SUMS.txt`.

- `ybx-s3-entry-compare-0029.log`, `ybx-entry-caller-0030.log`: correct caller/callee window and distinct objects.
- `ybx-home-action-machine-0032.log`: authentic guest action index 4 from callback through activation.
- `ybx-home-render-after-more-0033.log`: `onMoreOpen` entered; no later HomeActivity render.
- `ybx-more-files-0034.log`: plugin directory functions return, requestUpdate reached and returned.
- `ybx-render-notify-0035.log`: render notification issued; no observed task wake.
- `ybx-rtos-notify-result-0036.log`: target task handle and **pdPASS** return.

QMP reports are adjacent to those logs, named `s3-x4pro-action-machine-0032.json`, `s3-x4pro-more-file-path-0034.json`, `s3-x4pro-render-notification-0035.json`, `s3-x4pro-notify-result-0036.json`. Do not commit factory ROM, flash or massive image artifacts.

## Next acceptance gate

Instrument FreeRTOS **render task TCB** at handle 0x3fcec420 (state, notification state/value, blocked/ready list), CPU core scheduling and interrupt delivery **after a successful xTaskGenericNotify**. Compare pre/post notification in unmodified factory firmware; isolate minimal failing guest task-notification reproduction before proposing QEMU timer/IPI/interrupt fixes. Once fixed, replay More tap **without debugger** and require a changed SSD1677 panel RAM SHA and an increased genuine `DISPLAY_START` frame counter. Task #38 remains IN PROGRESS.
