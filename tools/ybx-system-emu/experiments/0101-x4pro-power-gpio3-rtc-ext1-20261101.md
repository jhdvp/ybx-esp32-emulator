# 0101 — X4 Pro virtual POWER button → digital GPIO3 + RTC EXT1 → native inkMOD DSLEEP

Date: 2026-11-01. Experimental branch `feat/x4pro-power-rtc-wake-20261101`, isolated QEMU binary derived from 0100. **No production deployment or replacement of the stable 8784 QEMU**.

## Board mapping is hardware-grounded

`CrossPoint-firmware/freeink-sdk/libs/hardware/BoardConfig/include/BoardConfig.h`, `BoardProfile XTEINK_X4_PRO`, specifies its **hardware-confirmed** digital button mapping `{..., up=GPIO0, down=GPIO7, power=GPIO3, powerActiveHigh=false}`. GPIO3 is the *physical POWER button* (active LOW) on this board. In 0100, unmodified inkMOD v1.1.10 independently configured `RTC_CNTL_EXT_WAKEUP1_SEL=0x8` (RTCIO3), ALL_LOW, EXT1 enabled. These observations match exactly. This does not mean a physical X4 Pro button was actuated in the QEMU tests.

## 0101 patch and corrected GPIO reset behavior

[`0101-x4pro-power-button-gpio3-rtc3-reset-retention.patch`](0101-x4pro-power-button-gpio3-rtc3-reset-retention.patch) applies to QEMU after 0100. It modifies only `hw/xtensa/esp32s3.c`, `hw/gpio/esp32_gpio.c`, and `include/hw/gpio/esp32_gpio.h`:

- Opt-in virtual board property `ybx-x4pro-power-button-pressed` accepts a **physical button event** (`pressed=true/false`), which drives a single active-LOW level simultaneously into the existing **digital GPIO3 input** and into the `RTC EXT1 PAD3` input of the same ESP32-S3. The property is rejected unless the opt-in X4 Pro board profile is active (`ybx-x4pro-gt911=on`). No QMP `system_reset`, synthetic RTC interrupt, forced RTC `deep-wakeup`, or fabricated device ACK is generated.
- **Real defect found by QTest**: QEMU reset originally restored `GPIO_IN` from pull-up values even when the externally driven Power button remained physically held LOW. This incorrectly turned the digital GPIO3 HIGH after a DSLEEP reset, while the RTC domain remained LOW. The revised digital GPIO model records *externally driven pin levels* separately from software output latches and re-applies them after digital reset. This is general to opt-in emulated GPIO outputs on S3, not a hard-coded fake input read for just GPIO3.
- Existing guest-programmed RTC EXT1 mask/mode/wake-enable gate from 0100 remains solely responsible for initiating the digital reset.

Experimental QEMU binary SHA-256: `7a3feb4ca30af038bc1b7f63165a45197fa298f26a93a87963b051dc245bfab3`; no QEMU binary, ROM, merged flash or SD image is committed. Source patch independently passed `patch --dry-run -p1` against the 0100 QEMU checkout.

## Real QTest against compiled experimental QEMU

`probe_x4pro_power_button_rtc_qtest.py` passed **25 MMIO operations**, JSON `/tmp/ybx-x4pro-power-0101/power-qtest.json`:

- Idle (released) virtual Power button is HIGH in digital GPIO3 and RTC pad3.
- Pressing button makes both digital GPIO3 and RTC pad3 LOW; no reset without `SLEEP_EN`.
- Wrong selected RTC pad7, wrong ANY_HIGH mode and disabled EXT1 **do not** trigger reset.
- With guest-style selected RTCIO3, ALL_LOW, EXT1 enabled and sleep armed, pressing virtual Power LOW triggers exactly **one** reset, EXT1 status `0x8` and both CPU reset causes **5** (`RTC_RESET_STATE=0x3145`).
- While the button is held through digital reset, the GPIO input **stays LOW**. Releasing it returns the digital input HIGH and does not cause another reset. The pre-fix test caught this issue; it now passes.
- `real_physical_button_verified=false`: these QMP events are virtual board level inputs.

## Real native firmware control and positive replay

`probe_inkmod_power_button_wake.py` launches original unmodified private inkMOD v1.1.10 merged flash + ESP32-S3 ROM + SD card, each on a private writable copy, offline. No flash or firmware edits, and no direct RTC reset or wake command.

| Scenario | Measured result |
|---|---|
| Guest before button input | RTC EXT1 selected mask `0x8`, mode ALL_LOW, guest sleep request present |
| No Power button signal | **0 RTC wakes, 0 EPD frames**; no panic |
| Press virtual X4 Pro Power `false → true` | Digital GPIO3 goes LOW; RTC EXT1 triggers once |
| After reset | `RTC EXT1_STATUS=0x8`, `RTC_RESET_STATE=0x3145`, UART `rst:0x5 (DSLEEP)` |
| After positive run | **5 SSD1677 frames**, 0 I²C `ESP_ERR_INVALID_STATE`, no panic |

The native 5 frames are only **post-wake graphics activity**; they do not by themselves prove which menu the long-held button selects, full navigation or realistic button debounce timing. Read-only JSONs at `/tmp/ybx-x4pro-power-0101/inkmod-power-not-pressed.json` and `inkmod-power-pressed.json`.

## Independent firmware controls

Original Factory X4 Pro still draws 4 frames and original visible SHA `e79c2ad5f2b60b16fe3f20625742d5f1343f5785552d1b32c11f33c42a874672`; original CrossPoint 2.0.0rc still draws 3 frames and original visible SHA `78e743450b8f67ced89eb59d86b7c9da61eb306b4c3e92c31f50b345ad99c296`. Both have zero panics/I²C invalid-state errors. Compiled QTests `probe_x4pro_rtc_ext1_gpio_qtest.py`, `probe_x4pro_gt911_short_reset_qtest.py` and `probe_x4pro_touch_injection_qtest.py` were each run against the 0101 experimental binary and all **PASS**. Host suite (with companion CrossPoint checkout) **920 tests, 10 skipped, exit 0**. Still not release-qualified for all devices, power states or real hardware.

## Limits and release gates

1. This is an opt-in **virtual board-level Power button**, not a real physical X4 Pro key, so its QMP button events must not be described as physical button testing. It models signal fanout; no host GUI button was wired to QMP during this experiment.
2. The emulator still does not halt/downgate the Xtensa cores for genuine low-power deep sleep; EXT1 reset response is more faithful, not a complete power/clock-state model. Other RTC wake sources (timer, EXT0, hold, RTC GPIO mux) still need work.
3. Do not promote to the stable 26-patch binary without composition/rebuild, original factory/CrossPoint navigation, GT911 EPD lifecycle and full no-regression suite. The user's live `8784` remains intact.
4. Don't clear unrelated build data: the development Mac was near 4 GiB free during the experiments. Use APFS copy-on-write for isolated QEMU snapshots.
