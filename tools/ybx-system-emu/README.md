# YBX Reader — system-level ESP32 firmware emulation

Status (2026-10-29): **Real CPU/bootloader/app execution works for both targets**.
**Full hardware emulation is NOT complete**. This test harness is intentionally
honest: it never calls a timed-out emulator process a complete integration pass.

## What is actually executed

The official `esp-emu` v0.51.0 binary executes the **real merged flash**
factory images built for YBX firmware (ESP32-C3 RISC-V and ESP32-S3 Xtensa LX7):
ROM, 2nd-stage bootloader, ESP-IDF/Arduino startup and the application machine
code. The images come from an independent local artifacts directory, **not
from synthetic HTML, nor a recompiled host build**.

The saved images used for the first test are the `092c2be5` test firmware,
not a final firmware release and not automatically rebuilt on new commits.

The harness:
- validates ROM/partition/app markers at 0x0 / 0x8000 / 0x10000;
- verifies and reports each input image's SHA-256 and size;
- runs the emulator with a bounded timeout, distinct UART log for each target;
- defaults to `--net user,restrict=yes`, preventing guest outbound TCP/UDP;
- **never** uses `--save-state` or flashes a device, so it cannot modify the
  original factory .bin;
- generates structured `report.json` with separate evidence for ROM, flash,
  application, I²C, SDMMC and **explicitly unverified** Wi-Fi/e-paper/TLS;
- can add restricted I²C register-file stubs for bring-up experiments; these
  are *not* GT911/RTC/gauge behavior simulations.

## Installation

On macOS Apple Silicon:
```sh
# Obtain official release/installer; review if security policy requires.
curl -fsSL https://raw.githubusercontent.com/espressif/esp-emulator/main/install.sh -o /tmp/esp-emu-install.sh
sh /tmp/esp-emu-install.sh --version 0.51.0
```

Verified installer checksum against official GitHub release assets before
installation. The installed binary is `~/.local/bin/esp-emu`.

## Run both saved firmware images

```sh
python3 tools/ybx-system-emu/run.py \
  --images-dir "$HOME/Projects/YBX/artifacts/ybx-reader-ui-20261029/settings" \
  --duration 10 \
  --out "$HOME/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/boot-10s"
```

You may pass `--c3-image /path/to/merged-factory.bin`,
`--s3-image /path/to/merged-factory.bin`, `--chip c3|s3|both`,
or `--i2c-stubs`. The CLI intentionally refuses ordinary app-only
`firmware.bin`: a CPU emulator requires **merged flash** with bootloader and
partition table. PlatformIO's `firmware.factory.bin` matches this layout.

Use `--allow-network` **only after** setting up a controlled test service,
test account and credentials; the guest firmware could contact production
endpoints. Merely allowing TCP does not prove TLS.

## Verified board hardware from BoardConfig.h

| Board | CPU | Display SPI | SD | I²C |
|---|---|---|---|---|
| X3 | ESP32-C3 RISC-V | UC8253/UC8279d, 792×528 | SD SPI on shared display bus | BQ27220 0x55, RTC 0x68, IMU 0x6B |
| X4 | ESP32-C3 RISC-V | SSD1677, 800×480 | SD SPI on shared display bus | none in default X4 profile |
| X4 Pro | ESP32-S3 Xtensa LX7 | SSD1677/UC8179/UC8279, 800×480 | **1-bit SDMMC** CLK41 CMD42 DAT0=40 | GT911 0x5D (alt 0x14), RTC 0x51, CW2017 0x63 |

The earlier browser-only demo labelled X3 as 480×800; that should not be
mistaken for the actual X3 panel dimensions **792×528**, subject to orientation.
The C3 firmware can select X3/X4 at runtime, which cannot be established
with a ROM-only boot log.

## Peripheral coverage / blockers

| Subsystem | Current observation | Required next implementation |
|---|---|---|
| ESP32-C3 CPU | ROM/boot/app machine code execute | firmware loop + watchdog/recovery |
| ESP32-S3 CPU | ROM/boot/app machine code execute | both cores/PSRAM, runtime health |
| SPI Flash | real boot from merged image | NVS read/write, OTA image selection and rollback |
| I²C | opt-in BM8563/CW2017/GT911 model and synthetic touch over QMP verified in compiled QEMU | real GPIO interrupt-matrix routing and touch-to-UI response |
| SPI e-ink | **Verified virtual SSD1677 pixel-frame capture** from real X4 Pro factory firmware via GP-SPI2 PIO, CS13/D-C18/RST14/BUSY6: 2 display updates, 48,000-byte nonblank 800x480 panel RAM and correctly oriented 480x800 YBX home screen | hardware waveform timing/accuracy, DMA/GDMA, UC8179/UC8279, application lifecycle and cross-client tests remain open |
| SD (X3/X4) | no verified virtual SPI card | SD SPI command/state and FAT file image |
| SD (X4 Pro) | **PROVEN:** native SDMMC DMA read/write, GPIO1/5 card power, guest FAT16 file creation in separate run; **same-run** SDMMC sector reads and nonblank SSD1677 EPD pixels | same-run FAT write, lifecycle and input integration |
| Wi-Fi | emulated AP/NAT implemented by ESP-EMU, **firmware not provisioned/connected** | controlled AP provisioning, DHCP, timeout and Wi-Fi fallback tests |
| TLS | crypto CPU/peripheral may be emulated, **zero YBX TLS handshakes verified** | isolated HTTPS test server with trust roots, failure cases, cert expiry, redirect/auth isolation |
| Buttons/touch | GT911 QTest protocol, contact/release/home and GPIO10 input feedback VERIFIED; strict real guest tap-to-new-SSD1677 UI **NOT PASSED**; diagnostic guest GT911 I2C counters and X4 Pro key pullups | actual RTOS task wake/poll, touch-to-UI frame, safe GPIO IRQ matrix routing (0020–22 quarantined) |
| End-to-end YBX | Factory firmware FAT file writes (separate run), same-run SDMMC reads + SSD1677 home UI **VERIFIED**, full device **NOT PASSED** | same-run FAT writes, touch-to-UI response, GPIO IRQ, DMA/GDMA, Wi-Fi/TLS and lifecycle tests |

**Why this is not a minor configuration task:** The current distributed
`esp-emu` CLI provides `--i2c-dev` for register files but exposes no
`--sdmmc-card`, `--spi-eink`, or external peripheral plugin API.
The official repository distributes precompiled binaries and helper scripts.
A complete virtual Xteink board therefore requires upstream emulator
support/new peripheral models or a separately extensible emulator core;
putting an SVG on top of UART does not satisfy the requirement.

See the machine-readable `report.json` and original UART logs under the
independent test artifacts path. No images or credentials are committed.

## Tests

```sh
python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py'
```

The unit tests verify classification (including that timeout != peripheral
success) and rejection of app-only .bin. CPU proof is backed by the actual
saved UART logs, not unit-test mocks.

**Safety:** Emulator bound to the local machine, no production network by
default. Do not treat virtual device behavior or tests as a replacement for
hardware validation on actual X3/X4/X4 Pro.
