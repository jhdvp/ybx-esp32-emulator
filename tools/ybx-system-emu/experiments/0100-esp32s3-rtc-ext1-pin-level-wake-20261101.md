# 0100 — ESP32-S3 RTC EXT1: guest-configured pin-level wake with DSLEEP reset

Date: 2026-11-01. **Isolated QEMU research**: `feat/rtc-ext1-level-20261101`, built from the local `0096+0097+0098` QEMU tree, **not deployed to the running port 8784**.

## Problem with the prior 0097 milestone

`0097` demonstrated that an **explicit** QMP command to `ybx-rtc-inject-deep-wakeup` restarts the original inkMOD 1.1.10 firmware from the ESP32-S3 ROM with reset reason 5 (DSLEEP). That does not represent a physical source: firmware had not exercised an emulated EXT1 pin/mode/mask. A fixed delay or an unconditional `SLEEP_EN -> reboot` would be wrong because deep sleep must remain asleep without the configured external event.

## Source-verified Espressif hardware contract

From the *installed ESP-IDF ESP32-S3* headers and HAL `hal/esp32s3/include/hal/rtc_cntl_ll.h`, and `esp_hw_support/sleep_modes.c`:

- RTC base `0x60008000`; `RTC_CNTL_EXT_WAKEUP1_REG` at `+0xE0`, selected RTC GPIO bitmap in bits `21:0`, status-clear write-one bit `22`.
- `RTC_CNTL_EXT_WAKEUP1_STATUS_REG` at `+0xE4` exposes RTC GPIOs that caused the wake; selection survives a digital deep-sleep reset.
- `RTC_CNTL_EXT_WAKEUP_CONF_REG` at `+0x64`: bit 31 chooses ANY_HIGH vs ALL_LOW.
- `RTC_CNTL_WAKEUP_STATE_REG` at `+0x3C`: `RTC_CNTL_WAKEUP_ENA` bit field starts at 15, and `RTC_EXT1_TRIG_EN=BIT(1)` corresponds to register bit `16`.
- `RTC_CNTL_STATE0.SLEEP_EN` is bit 31; RTC IO channels 0–21 map to ESP32-S3 GPIO0–GPIO21 by `esp32s3/rtc_io_periph.c`.

Test patch [`0100-rtc-ext1-gpio-level-wakeup.patch`](0100-rtc-ext1-gpio-level-wakeup.patch) adds the registers, a named 22-line RTC pad input and QOM-observable counters. It invokes digital reset **only** when `SLEEP_EN`, EXT1 wake enable, nonzero selected mask and matching *known external pad levels* coincide. ANY_HIGH accepts one known selected high pad, while ALL_LOW requires *every* selected pad to have a known LOW level; an unconnected/unknown pad does not invent a wake. Correctly writes reset cause 5 to both CPU cores and retains EXT1 wake status until W1C. It does not affect Wi-Fi, I²C, EPD or firmware bytes.

The only external input currently connected is the **test** QMP property (`ybx-rtc-ext1-test-pin`, `ybx-rtc-ext1-test-level`) which drives the named RTC device input. This does **not** yet wire the virtual board's actual X4 Pro power/Home/side-button GPIO lines into the RTC power domain. Do not call the complete hardware IO routing implemented.

## Real QTest on compiled experiment

`probe_x4pro_rtc_ext1_gpio_qtest.py` ran against the 0100 compiled QEMU, with genuine QTest MMIO and QMP test pad input. **PASS: 30 QTest operations, two qualifying DSLEEP resets**. Assertions include:

- Input before firmware configuration does not cause reset.
- `SLEEP_EN` without EXT1 enable cannot wake.
- Wrong pin, low pin in ANY_HIGH mode, and missing selected levels cannot wake.
- ANY_HIGH selected high pad causes RTC reset with both core causes = 5 and EXT1 status matching that pin.
- W1C status clearing preserves selected mask, with no second reset outside sleep.
- ALL_LOW with two pins does **not** wake while one is HIGH, but does when both are LOW.
- Disabling EXT1 or zeroing selected mask blocks wake; invalid RTC pin 22 is rejected.

Result: `/tmp/ybx-rtc-ext1-0100/ext1-qtest.json` on the development Mac. Binary SHA-256 `77b77ccda255cf6b25b7f7e70d401bb1ac07bdbea1523476a0b1d24fa3a8120b`. The source patch applies cleanly after the 0098 research build; no precompiled QEMU binary is committed.

## Native original inkMOD 1.1.10 — exact observed EXT1 configuration

`probe_inkmod_ext1_wake.py` launched the unmodified private merged firmware + ESP32-S3 ROM + pristine SD under an isolated, disposable QEMU process. No HTML replay, firmware editing, synthetic I²C ACK or unverified guest code changes.

First **negative control**, no RTC pin driven:

| Observed live guest register/metric | Value |
|---|---|
| `RTC_CNTL_EXT_WAKEUP1_SEL` | `0x8` (**RTC GPIO3** only) |
| `RTC_CNTL_EXT_WAKEUP_CONF` | `0x0` (**ALL_LOW**) |
| `RTC_CNTL_WAKEUP_STATE` | `0x00010000` (**EXT1 enabled**) |
| `RTC_CNTL_STATE0.SLEEP_EN` | Guest requested deep sleep |
| Native RTC EXT1 wake counter | **0** |
| Native SSD1677 frames | **0** |

Second **positive control**: set `RTC GPIO3` to **LOW** through the external pad-level test input, without sending any `ybx-rtc-inject-deep-wakeup`, QMP `system_reset`, timer delay reset, or fake wake flag:

| Observation | Verified value |
|---|---|
| Input | `RTC GPIO3 LOW` |
| `ext1-wake-events` | `0 → 1` |
| `EXT_WAKEUP1_STATUS` | `0x8` |
| Digital reset cause register | `0x00003145` — both cores cause `5` |
| UART ROM | `rst:0x5 (DSLEEP)` |
| SSD1677 frames after native boot | **3** |
| UART `ESP_ERR_INVALID_STATE` | **0** |
| Panic / abort | **None** |

The successful result reproduces the previous inkMOD 3-frame milestone but now through the **guest-programmed RTC EXT1 source**, not an explicit deep-wakeup/reset command. Input was externally stimulated by QMP, as a physical GPIO press would be; routing a real virtual board pin to RTC remains an outstanding board-level feature.

Control JSON: `/tmp/ybx-rtc-ext1-0100/inkmod-ext1-control.json` and positive JSON: `/tmp/ybx-rtc-ext1-0100/inkmod-ext1-pad3-low.json`.

## Factory/CrossPoint no-regression

The same isolated 0100 binary booted original Factory X4 Pro with **4 frames**, original home SHA `e79c2ad5f2b60b16fe3f20625742d5f1343f5785552d1b32c11f33c42a874672`, and CrossPoint 2.0.0rc with **3 frames**, original home SHA `78e743450b8f67ced89eb59d86b7c9da61eb306b4c3e92c31f50b345ad99c296`. No panic or I²C errors on either guest. All private ROM, flash and SD inputs remain outside Git.

## Limitations / next acceptance gates

1. Connect the virtual X4 Pro GPIO3 input to RTC IO channel 3. **Hardware-confirmed mapping found:** `CrossPoint-firmware/freeink-sdk/libs/hardware/BoardConfig/include/BoardConfig.h`, `XTEINK_X4_PRO` (lines 1789–1830), documents **Power=GPIO3 active-LOW**, Left=GPIO0 and Right=GPIO7; thus the guest-selected RTC GPIO3 is specifically the X4 Pro POWER button. This is evidence of the board pinout, **not** proof that a virtual power-button event is already connected to RTC. The current QMP test input is not a physical button and bypasses the regular GPIO input path. A browser power-button event must propagate through the real GPIO model and then EXT1, with QTest proving no accidental power-on strap/button regressions.
2. Model power-domain sleep state explicitly; QEMU CPUs currently continue running the guest's waiting loop until the RTC external input triggers the reset. Passing the RTC register tests does not make deep sleep power consumption or CPU gating hardware-faithful.
3. RTC timer wake, EXT0, per-pad RTC IO mux, IO hold and analog RTC wake sources remain unimplemented. Do not claim them as supported.
4. This patch is a *research candidate*, not added to the stable audited 26-patch manifest, the `8784` launcher, or the default public build. Host QMP-only wake test inputs should be flagged and isolated from real device behavior.
