# ESP32-S3 ROM DRAM experiment — NOT A FIX

Date: 2026-10-29. Source: Espressif QEMU `febae18`.

Hypothesis: QEMU loads only `esp32s3_rev0_rom.bin` into instruction ROM, but the toolchain ROM ELF includes a 1460-byte DRAM segment at `0x3fcd7000`. Missing ROM data might cause the previously observed debug exception at `0x40049057` (`usb_uart_device_tx_one_char`).

Change in `0002-s3-rom-dram-experiment.patch`: load the local ROM ELF's 1460-byte DRAM segment separately through QEMU's `load_image_targphys_as`. The external binary `esp32s3_rev0_rom_dram.bin` was generated locally and must **not** be committed. This is an exploratory QEMU patch only, not applied to firmware.

**Observed result:** QEMU built and executed with the additional ROM data. The instruction trace still contains the debug exception vector at `0x40000280` / `0x40000288` and the same USB UART function at `0x40049057`. There is no YBX UART output and no application startup evidence; the process timed out after seven seconds.

Trace: `~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/qemu-boot-diagnostics/s3-dram-fixed-exec.log`.

**Decision:** Experiment rejected as an effective boot fix. Patch reverted from the actual QEMU worktree and retained here only as a reproducible hypothesis. Do not deploy, assume ROM completeness, or count this as a passing emulation gate.

Next: investigate why ROM UART's output pointer is invalid/exceptional by inspecting ESP32-S3 ROM runtime initialization, reset peripherals and specific exception registers; avoid bypassing ROM initialization.

## 0004 — forced DRAM initializer load (rejected)

`0004-s3-rom-initializer-trial.patch` is a discarded experiment against
Espressif QEMU's `hw/xtensa/esp32s3.c` (base `febae18`, after accepted
C3 GDMA + USB Serial/JTAG patches).

The patch loads generated local ROM writable-section values into RAM before
Xtensa CPU reset. HMP inspection confirmed that the RAM pointer at
`0x3fceffe4` was initially `0x3fcef6a4`, but ROM reset overwritten it
with zero, leaving the same `LoadProhibited` at `0x400494ff`.
It must NOT be added to `apply_qemu_patches.sh`.

**Superseding working solution:** `../extract_rom_with_sections.py`
reconstructs the guest ROM's **copy-source addresses** using the local
Espressif ELF; the original (unchanged) accepted-patch QEMU then proceeds
beyond the previous fatal ROM handler. Evidence and remaining acceptance
gates are in `../QEMU_BRINGUP.md`. No hardware or production firmware
was changed.

## 0006 — minimal S3 ADC one-shot handshake (experimental, not accepted)

`0006-s3-adc-oneshot-handshake.patch` applies on top of accepted patches
0001, 0003 and 0005 against isolated Espressif QEMU. Its C code implements
only the SENS ADC1/ADC2 `MEAS_CTRL2` start/done-bit handshake, with dummy
zero result and reset-state handling. It is **not a valid electrical ADC**,
and must not be used for accuracy or hardware validation.

With the exact factory X4 Pro ROM correctly reconstructed (including
`.rodata.interface`), normal QEMU stalls inside ESP-IDF
`read_cal_channel()`, waiting for `SENS_MEAS1_DONE_SAR` bit 16 at
`0x6000880c`. This experiment lets the guest progress to repeated
ESP-IDF 5 I2C `ESP_ERR_INVALID_STATE` errors. The experimental patch
compiles, and 18s guest tracing demonstrates progress from ADC calibration
to I2C. It remains **outside** `apply_qemu_patches.sh` pending a full
faithful ADC model. The live isolated local QEMU build currently includes it
for diagnostic experiments; the accepted upstream checkout does not.

See `../QEMU_BRINGUP.md` for evidence, local ROM hashes, flags and
remaining acceptance blockers. Never change production YBX firmware to
work around virtual hardware behavior.

## 0007 — ESP32-C3/S3 I2C master integration (experimental)

`0007-c3-s3-i2c-master.patch` reuses QEMU's existing classic ESP32
I2C model, with an explicit C3/S3 command encoding mode:
RESTART 6, WRITE 1, READ 3, STOP 2, END 4 (ESP-IDF 5). The classic
ESP32 command encoding remains the default and is unaffected.
It adds register `I2C_FIFO_ST` at 0x14, routes C3 I2C0 and
S3 I2C0/I2C1 through their real MMIO/IRQ addresses, and checks
the final command register index boundary.

The patch is a **diagnostic experiment**, not yet an accepted QEMU
hardware model. It is based on the isolated QEMU tree **with**
`0006-s3-adc-oneshot-handshake.patch` already applied. Apply 0006
before 0007. Both targets compiled successfully with Meson/Ninja.
`probe_i2c_qtest.py` passed on both real compiled QEMU binaries,
testing no-slave NACK, IRQ, clearing IRQ, command DONE,
and RESTART/STOP opcodes. Real S3 YBX firmware MMIO trace verifies
the firmware sends GT911 address 0x5D, receives NACK, and handles IRQ;
it does NOT establish actual GT911 behavior. C3 aborts via an
interrupt watchdog before accessing I2C. Logs and JSON reports
are documented in `../QEMU_BRINGUP.md`.

Keep absent I2C slaves absent. The X4 Pro requires separate
BM8563 RTC at 0x51, CW2017 at 0x63 and power-gated GT911 at 0x5D
models, each with faithful protocol/interrupt/power semantics.

## 0008 — C3 ADC one-shot bit handshake (experimental)

`0008-c3-adc-oneshot-handshake.patch` is an **unaccepted**
RISC-V/C3-only prototype on top of experiment 0007 in isolated
Espressif QEMU. It maps C3 `APB_SARADC_ONETIME_SAMPLE` 0x60040020,
`INT_RAW` 0x60040044 and `INT_CLR` 0x6004004c and reports ADC1
DONE only after START. It always returns a dummy zero sample.
It neither models analogue input nor proves ADC calibration or
guest application readiness.

Real compiled-QEMU MMIO QTest passed via
`probe_c3_adc_qtest.py`; full 58/58 host tests pass. Guest UART
showed no previous watchdog within a 10-second bounded run, but also
did not reach normal YBX app UART in that window. Exact source
revision was rebuilt, but C3 factory IROM did **not** binary-match;
do not overinterpret C3 ELF line mappings. See
`../QEMU_BRINGUP.md` for logs, hashes, register semantics, and
strict boot acceptance requirements.

## 0009 — X4 Pro BM8563 RTC + CW2017 gauge, explicit S3 machine opt-in

`0009-s3-optin-bm8563-cw2017.patch` introduces
`hw/i2c/ybx_x4pro_i2c.c` and uses Espressif QEMU's existing
I2C bus model from 0007. Do **not** add these virtual devices to
generic S3 by default. To enable this simulation:

```text
-machine esp32s3,ybx-x4pro-i2c=on
```

The option belongs to **-machine**; `-global xtensa.esp32s3...`
does not work (QEMU reports an invalid global class). The YBX
emulator runner now supports `--x4pro-i2c` and records its
attachment in `report.json`. C3 rejects the option. QTest
exercises both negative and positive configurations and full
I2C register read/write and CW2017 80-byte profile upload.
It does not imply verified hardware/boot readiness.

Modes and constraints: synthetic 70% battery/3800mV (overridable
on the CW2017 QEMU device), real host-synchronized BCD RTC with
basic writable registers, no charger GPIO21 status, no GT911,
no power gating or I2C wake interrupts, no power-accurate analogue
inputs. See `../QEMU_BRINGUP.md` for validated chip addresses,
QTest evidence, I2C device limitations and boot/acceptance status.
QEMU's current `esp32_gpio.c` write handler is a stub; proper
GT911 simulation requires GPIO2 (active-low power), GPIO4 (reset),
GPIO10 (interrupt) behavior first.

## 0010 — opt-in GPIO2/4/10 output and enable MMIO

`0010-s3-optin-gpio-output-registers.patch` (after 0009) adds
S3 pin output callbacks and basic GPIO_OUT/ENABLE/W1TS/W1TC MMIO
to the otherwise no-op Espressif GPIO parent model.
Only enabled with `-machine esp32s3,ybx-x4pro-i2c=on`.
Tested against compiled QEMU with `probe_x4pro_gpio_qtest.py`:
normal S3 still ignores writes, opt-in S3 updates both GPIO
register banks, preserves boot strap and toggles GPIO2/4/10
direction/output correctly. This is GPIO MMIO groundwork only.

## 0011 — GT911 GPIO power/reset/address I2C verified; input still pending

`0011-s3-optin-gt911-power-gating.patch` (after 0010)
compiles an optional Goodix GT911 model on S3 I2C0, with GPIO2
active-low power, GPIO4 reset, GPIO10 address selection, no
fabricated touches and idle status at 0x814E. Its real compiled-QEMU
**deterministic QTest passes** after fixing the runner to use
`-accel qtest` alongside `-qtest stdio`. This unlocks the
`clock_step` command, previously rejected due to missing
QTest accelerator. The script checks 0x5D/0x14, NACK while
unpowered/in reset, minimum reset and ready delays, product
registers and no contacts. JSON evidence
`qa-20261029/s3-x4pro-gt911-qtest.json`.

A standalone YBX runner opt-in `--x4pro-gt911` requires
`--x4pro-i2c`; the generic S3 default is unchanged. This
validates **synthetic device handshake and MMIO behavior**, not
touch-event injection, input GPIO interrupts, full Goodix setup,
e-ink panel output, SD mount or full guest ready. 0011 remains
EXPERIMENTAL rather than added to the default patch chain.
See `../QEMU_BRINGUP.md` for the IROM-execution evidence and
exact factory/build SHA matching.

## 0012 — synthetic touchscreen events and GPIO10 level feedback (QTest/QMP)

`0012-s3-gt911-touch-injection-gpio-feedback.patch` (after 0011)
provides opt-in `/machine/soc` QMP properties
`ybx-touch-x` / `ybx-touch-y` / `ybx-touch-state` for
deterministic synthetic GT911 single-contact and HOME events.
It implements 0x814E READY/CONTACT/HOME status, 0x8150 eight-byte
contact data, guest acknowledgment, and GPIO_IN[10] when the
pin is configured as input. State 0=release, 1=touch press,
2=HOME press, 3=HOME release. The raw portrait coordinate bounds
are checked. It is only a simulator interface, **not real hardware**.
The live offline Docker QTest+QMP probe `probe_x4pro_touch_injection_qtest.py`
passed 235 MMIO commands in the same QEMU instance, with product
data/clock/status readback and output hashes in the QA directory.
Does not prove GPIO edge interrupt-matrix dispatch, GUI response,
EPD pixels or SD mount. Leave those tasks open.

## 0013 — actual X4 Pro GPIO1 master-power prerequisite for GT911

`0013-x4pro-gt911-master-rail-gpio1.patch` (after 0012)
makes GT911 power dependent on both GPIO1 HIGH (board master
peripheral rail) and GPIO2 LOW (touch enable). It handles
reset held low across power-up and resets device state when
either rail drops. Live compiled QEMU confirms NACK without
GPIO1 and after dropping it, plus normal touch/QMP injection.
It does not emulate full board PMIC behavior. See the
timestamped 22-read QTest and 247-command injection reports
in `QEMU_BRINGUP.md`.

## SDMMC: no extra patch needed for verified command and sector DMA

The existing Espressif QEMU S3 DesignWare controller at 0x60028000
already implements CMD0/8/55/41/2/3/7/16/17/24, authentic
card responses and IDMAC read/write. See
`probe_x4pro_sdmmc_qtest.py` and
`probe_x4pro_sdmmc_sector_qtest.py`; the latter is an
offline in-Docker test on a copied FAT image. Source-sector
SHA and written/read-back sector SHA match exactly. There is
no new hardware model or patch. Still need verified firmware
FAT mount, GPIO5 active-LOW SD power gating, and e-paper SPI.
Do **not** describe these low-level tests as a full app boot.

## 0014 — board-power-aware SDMMC and observed guest FAT16 file writes

Apply `0014-x4pro-sdmmc-board-power-gate.patch` after 0013.
Opt-in S3 machine property `ybx-x4pro-sd-power=on` requires
GPIO1 HIGH + GPIO5 LOW for actual SD card commands; power cycling
resets the SD protocol. Default machine unaffected and combined
GT911/SD GPIO1 fanout passes compiled QTest.

The original X4 Pro firmware executes SdFat with MBR partition
#1, NOT a FAT16 superfloppy. Host helpers
`make_sd_mbr_image.py` (safe 128MiB MBR-partitioned image)
and `analyze_guest_fat_image.py` (post-guest FAT chain and BMP
evidence) are integrated with `run_open_qemu.py --s3-realtime
--sd-original-fat-image`. Factory guest created an actual
Screenshots directory and nonblank 1bpp BMP on SD; FAT mount
is VERIFIED by file creation, not inferred from CPU or UART.
Full EPD display and app lifecycle remain unverified.

## 0015 — experimental GP-SPI2 CPU-controlled transaction transport, NOT EPD

`0015-s3-optin-gpspi2-transaction-scaffold.patch` adds
opt-in S3 GP-SPI2 MMIO at 0x60024000, distinct from the
SPI1 flash/Octal PSRAM at 0x60002000. Supports CPU FIFO
`SPI_W0..W15`, MOSI data length from MS_DLEN, and
self-clearing USR/UPDATE operation bits. QTest/QMP checks
one 0x11,0x22,0x33,0x44 transfer and generic-machine
negative probe; real original X4 Pro factory firmware
issued 11,496 PIO transfers carrying 720,246 bytes in
one bounded 18s run. Real firmware UART readiness, slave
display state, CS/DC/RESET/BUSY GPIO, DMA/GDMA and
EPD pixels are all NOT proven. Keep #38 in progress.
No production firmware changes and no default opt-in.

## 0016–0018 — virtual X4 Pro SSD1677 via actual GP-SPI2 + GPIO, guest UI capture

Apply in order after 0015:
- `0016-s3-x4pro-ssd1677-panel-gpio-frame.patch` — opt-in
  `ybx-x4pro-epd=on` SSI slave connected to the S3 GP-SPI2 controller,
  CS13/D-C18/RST14/BUSY6 and true 48KB mono panel RAM with QMP visibility.
- `0017-ssd1677-guest-ram-telemetry.patch` — QMP reads for
  0x24/0x26 command counts and nonwhite RAM byte metrics, for
  diagnosing guest resets vs real screen content.
- `0018-ssd1677-10bit-pixel-ram-refresh-gating.patch` — required
  hardware-correct SSD1677 10-bit X pixel coordinates (0x44 uses
  FOUR bytes, 0x4E TWO), X+8 byte advance with Y decrement, 0x22
  DISPLAY_START bit2 gating before 0x20 counts as refresh.
  Without 0018 the genuine guest panel remains incorrectly white.

Compiled-QEMU QTest asserts CS/DC/reset, pin6 BUSY, full 800x480 RAM,
0xC0 power-on DOES NOT refresh, 0xF7 refresh DOES, and X pixel
boundary x=792 followed by row wrap preserves both payload bytes.
Actual original 092c2be5 factory app produced two valid display
updates with 4,743 nonwhite panel bytes and recognizable YBX
home screen over GP-SPI2, without any test-supplied UI or SD-BMP
shortcut. `probe_x4pro_spi2_guest.py --epd` captures true SSD1677
RAM via QMP and, on nonblank frames, generates a 480x800 upright
PNG using lossless conversion in `epd_frame_io.py`.
QEMU reports and image under `qa-20261029/s3-x4pro-ssd1677-guest-0018-33s*`.

These patches remain experimental. DMA/GDMA, UC8179/UC8279,
physical EPD waveform accuracy, and real guest input-to-frame
state changes are NOT implemented or accepted.

### Same-run SD + EPD acceptance scope

The real factory S3 guest with SSD1677 opt-in and GPIO1/5-powered
SDMMC produced 32 native SD card commands/6 sector reads and a
nonblank virtual panel frame in **the same guest execution**.
See `s3-x4pro-ssd1677-guest-sd-trace-0018.json` and
`s3-x4pro-ssd1677-guest-sd-trace-0018.sdtrace.log`.
It did not write new SD sectors in this run, so the stricter
same-run FAT file-creation gate is still open. Separate #37 guest
FAT16 file-writing proof remains valid. Full user input/UI,
alternate panel controllers and physical E-Ink waveform accuracy
are NOT verified.

## 0019–0022 — GT911 guest readback counters, GPIO interrupt wiring, X4 Pro buttons

Apply after 0018, in order:
- `0019-s3-gt911-guest-i2c-readback-counters.patch`: opt-in QMP
  counters for actual guest reads 0x814E, first point 0x8150, ACK
  clear and synthetic events. Important to distinguish QMP injection
  from true guest handling.
- `0020-s3-gpio-input-status-irq-matrix.patch`: S3 GPIO_PINn
  MMIO interrupt type/enable, GPIO_STATUS0/1 W1TC and IRQ routing
  to `ETS_GPIO_INTR_SOURCE`. Edge IRQ latched until W1TC.
- `0021-x4pro-digital-button-pullups.patch`: board-specific
  GPIO0/3/7 active-LOW keys read HIGH in idle. Original QEMU
  input defaults were LOW and mimicked three held keys.
- `0022-s3-gpio-level-irq-deassert.patch`: essential fix;
  GPIO6 SSD1677 BUSY triggered a permanent CPU1 IRQ storm
  under the first 0020 implementation. Level interrupts now
  track the current physical pin level and automatically clear
  after BUSY falls; edge status still requires W1TC.

Compiled QTest covers GT911 I2C + GPIO10 edge status and true
GPIO6 EPD BUSY high->low level deassert. The earlier unsound
natural splash-to-home transition is NOT accepted as touch-to-UI;
live factory 092c2be5 test requires actual new guest 0x8150 reads,
GT911 ACKs, stable home *before* injection and subsequent changed
SSD1677 image. Do not enable these patches in default machine.

### 0020–0022 quarantine: NOT for a real guest, 0023 standalone safe pullups

A real factory guest confirmed GPIO6 EPD BUSY config
`GPIO_PIN6_REG=0x2180` (BOTH_EDGES, INT_ENA=1). The experimental
0020 interrupt matrix route latches GPIO_STATUS0 bit6,
traps FreeRTOS CPU1 in an IRQ loop and prevents the full home
screen. Patch 0022 only fixes HIGH/LOW **level** interrupt
deassert; the actual guest pin6 is configured for **edge**
interrupts. Do not enable/accept 0020/0021/0022 by default.
These patches are retained exclusively for reproducible
research, and NOT applied in the working isolated QEMU source.

`0023-x4pro-standalone-idle-button-pullups.patch` is a safe
independent replacement for the key pullups from 0021,
which otherwise depends on 0020. Apply 0023 after 0019
WITHOUT 0020–0022: X4 Pro keys GPIO0, GPIO3, GPIO7 idle
HIGH even after reset; QTest verifies actual GPIO_IN bits.
This cannot, by itself, establish full guest-to-UI touch.

## 0024 — SYSTIMER read-only comparator telemetry

`0024-s3-systimer-readonly-comparator-counters.patch` is
reversible, read-only diagnostics of real S3 timer callback fires
and comparator reprogramming. Confirmed guest FreeRTOS tick
activity on both active comparators, while GT911 guest I2C
read and 2 ACKs worked but YBX home did not change.
Corrected raw digitizer coords: More (350,760), Library
(130,760). Strict home-loaded 150ms More tap remained
`true_guest_touch_to_ui_verified=false`.
QA: s3-x4pro-systimer-guest-0024.json and
s3-x4pro-gt911-correct-more-0024.json.
GPIO IRQ patches 0020–22 remain quarantined; safe 0023
remains applied independently of them.

## 0025 correction and 0042 — actual GPIO6 IRQ fix

The previous 0025 hypothesis of YbxHomeUi/UiAppHost +236-byte
pointer corruption was INCORRECT: GDB compared HomeActivity
and YbxHomeUi, two different C++ objects across Xtensa `entry`
window rotation. Matched ELF tracing showed original factory
More `onAction=4`, selectedAction=4, HomeActivity accepted4,
onMoreOpen ran and xTaskGenericNotify returned pdPASS.
Do NOT patch Xtensa register-window instructions based on 0025.

Renderer held RenderLock and blocked on FreeInk's
`s_epdRefreshDone` binary semaphore `0x3fcec294` awaiting
the GPIO6 EPD BUSY completion ISR. Without a GPIO ISR, waiting
30 seconds timed out and *then* rendered the genuine More
popup. 42s-after-tap control run proved this (frame SHA
`8b81cec46b2dd5690cc2748d80d01a7ee86ca7ffbb2d57c7a92bcb68354222cf`).

`0042-s3-epd-busy-gpio-pcpu-irq.patch` is a replacement, NOT
an extension to incompatible 0020/0022. It implements:
- GPIO PINn interrupt type/enabled, input edge/level, STATUS W1TC;
- **PCPU_INT 0x5C** and PCPU_INT1 0x68 — actual ESP32-S3
  `gpio_ll_get_intr_status` read path in ESP-IDF, missing in 0020;
- QEMU intmatrix GPIO level IRQ connection only for experimental
  X4Pro SSD1677 EPD opt-in; default SoC unaffected.

Built in offline QEMU. Compiled QTest covers both GPIO6
BUSY edges at PCPU_INT and W1TC acknowledgement and SSD1677
frame/protocol. Original unmodified factory app under true S3
guest CPU now renders the More menu in **≤2 seconds after
150ms touch/release**, without GDB or artificially giving a
guest semaphore. Actual 1 GT911 coordinate read, 2 status ACKs
and SSD1677 displayed frame SHA change verified. QA:
`s3-x4pro-gpio6-pcpu-irq-0042-2s.json`,
`s3-x4pro-epd-0042-pcpu-edge-qtest.json`.
Host tests and patch reverse-apply acceptance required for promotion.
No physical e-paper timing guarantee. Kaneo #38 remains open for
full panel/UI and adjacent platform support.

## 0048 — Wi-Fi radio capability inventory: silent zero-fill, no radio model

Host-only measurement, no guest boot. `wifi_radio_inventory.py` reads the factory
X4 Pro ELF's own `l32r` literal pools (816 radio symbols; WIFI_MAC page
0x60033000 154 accesses/90 words, WDEV 0x60035000 125/32, FE 0x60006000 125/62,
MODEM_COEX 0x60031000 35/16, crypto key entries 0x60034400) and classifies the
same windows against `info mtree`. `probe_x4pro_wifi_radio_qtest.py` then reads
those registers in the prebuilt S3 machine: every radio word returns 0x0, while
controls `esp32s3.apbctrl` date, `open_eth` and the WDEV RNG page answer non-zero.

Cause: `esp32s3_io_read()` zero-fills 0x60000000..0x600D0FFF and its warning is
compiled out (`esp32s3.c:118,532-549,750-753`), so the driver waits forever on
clear status bits instead of faulting. Radio IRQ source 0 is already claimed by
the EMAC (`esp32s3_intc.h:26,150`), the S3 "modem" precedent in upstream is a
clock stub (`hw/misc/esp32c6_modem.c`), and `-device help` has no radio type.
`wired_eth` is 1 symbol, so the working `open_eth`+slirp path is not usable by
this image. Staged roadmap (tap → scan → auth/wdev crypto → DHCP → TLS → TSF)
and the 802.11-versus-bridge analysis are in `0048-batch2-wifi-radio.md`.
No scan, association, IP address or frame was produced or claimed.

## 0048 — multi-step navigation acceptance (host tooling only)

`0048-batch2-gui-navigation.md` extends the accepted single Home → More
tap into an ordered chain (More → YBX Settings → System settings → back)
with per-step SSD1677 image evidence. Pure host side:
`../x4pro_guest_navigation.py` (measured screen predicates and per-step
gates), `../run_x4pro_guest_navigation.py` (one boot, N taps, per-step
PNGs and SHAs) and `../test_x4pro_guest_navigation.py`. Tap coordinates are
measured from genuine guest captures, never from a web mock. The live
multi-step guest run was NOT executed here; More → Settings and the back
tap remain proven only through the shared browser `/api/tap` artifacts of
0043, which lack per-step I2C and refresh counters.

## 0051 — BUSY telemetry reset-order fix, rebuilt and re-verified

0037's counter clear ran before `epd_set_busy(s, false)` in `epd_reset()`, so a
`system_reset` inside a BUSY-high window recorded a fall whose rise had just
been erased and set `busy-high-ms` to the whole VM uptime. Reproduced on the
compiled binary (`rise=0, fall=1, high_ms=620` at a 620 ms VM clock), fixed by
`0051-busy-telemetry-reset-order.patch` (clear after the release; correct the
GPIO6 pullup comment), rebuilt and re-tested (`0, 0, 0`). Factory guest
Home → More → YBX Settings → System settings → Back still matches the accepted
frame SHAs. Details and evidence:
`0051-busy-telemetry-reset-verification-20261030.md`.

## 0052 — verification pass over both six-agent batches

All 14 batch reports were re-checked against the tree and against a running
emulator: host suites (312 / 288 tests), the 0043 EPD model test recompiled,
SSD1677 + GT911 + telemetry QTest on the rebuilt binary, patch-composition
guardrails, and the factory guest driven end to end. Batch-1 verdicts:
acceptance-probe tooling confirmed, its 30 s TCB framing not reproduced;
cross-core IPI chain confirmed, counters synthetic-only; epd-busy 20 ms
fallback confirmed twice; freertos-tcb offsets confirmed, live state still
unproven; qemu-epd-fix re-run; render-pipeline stays rejected. The pass also
found and fixed a snapshot bug (running tasks were invisible). Matrix, gaps and
reproduce commands: `0052-six-agent-verification-pass-20261030.md`.

## 0060 — Wi-Fi radio: factory register accesses pinned, non-fake stub proposed

`0060-batch3-wifi-radio.md` inventories the radio windows the factory X4 Pro
image actually touches (WIFI_MAC, WDEV, FE, MODEM_COEX, crypto) from the ELF
literal pools and pins the numbers in host tests. `0060-s3-wifi-radio-model.patch`
proposes an opt-in device that freezes status semantics instead of faking an
association; it applies cleanly to the pinned checkout but is not in the running
binary. The radio is still not emulated: no frame, scan, association, address,
DHCP or TLS is produced. Staged roadmap: register stub -> scan -> auth/crypto ->
DHCP/IP over slirp -> TLS -> TSF/coex.

## 0061 — SSD1677 waveform, deep sleep and BUSY around reset

`0061-batch3-epd-waveform.md` characterises the three known panel gaps in a
host model that mirrors `ybx_ssd1677.c` line by line: one 350 ms window for
both power-on and pixel refresh, no state for the deep-sleep command 0x10, and
BUSY driven low on reset where a real panel trails it high.
`0061-ssd1677-waveform-sleep.patch` proposes the fix and applies cleanly to the
pinned checkout; the running emulator still uses the shipped model.

## 0062 — multi-step GUI navigation acceptance

`0062-batch3-nav-automation.md` makes the acceptance chain runnable: Home ->
More -> YBX Settings -> System settings -> Back with the measured taps
(350,760), (240,453), (210,679), (35,61), a screen predicate and an expected
SSD1677 SHA per step, plus failure diagnostics. Browser mode needs an explicit
`--base-url` so it can never fall onto the shared instance by accident. The
runner was executed against the live instance: 4/4 steps passed with the
accepted SHA chain.

## 0063 — reproducible QEMU composition and build

`0063-batch3-repro-build.md` and `build_reproducible_qemu.sh` compose the
audited chain (0042 + 0037 + 0043 + 0051) from the pinned base `febae18` into a
fresh tree, sign the sources and patches, and can build
`qemu-system-xtensa` inside the pinned image. `check_qemu_patch_composition.py`
now treats the telemetry patches as part of the verified set and pins the
current binary SHA. Composition and manifest comparison are verified; the
container build is opt-in and has not been run end to end yet.

## 0064 — browser bridge hardening

`0064-batch3-browser-hardening.md` adds a read-only `/api/telemetry` endpoint
(BUSY edges, BUSY duration, frame counters, frame SHA - unavailable is
reported as unavailable, never as zero), ETag/Last-Modified caching on
`/screen.png` with 304 replies, a bounded and serialised tap queue with typed
400/429/503 answers, and a stricter private-bind predicate with a loud
no-authentication warning. Verified against the running instance: live counter
values and a 304 reply.

## 0067 — firmware picker: boot the operator's own build, honestly

`0067-batch3-firmware-picker-20261031.md` adds a second in-page picker for the
guest firmware image, mirroring the ROM picker: a read-only folder on the Mac
(`YBX_FIRMWARE_DIR`), `GET /api/firmware`, `POST /api/firmware`, and a page
control with the active image, its SHA-256 and its provenance. Images are
classified by their bytes (ESP flash layout, FAT/SD signature) and a file that
is not a valid ESP flash image is refused before the guest is touched.
`/api/state` no longer hardcodes `factory_guest: true` - it is derived from the
active flash SHA-256, so a custom build is never presented as the factory
firmware. Verified live: the operator's own builds boot and paint, the
application-only image and any path traversal are refused, and the upstream
CrossPoint and inkMOD images are documented as not booting yet (the crash
follows the CrossPoint-built bootloader, not the application).
