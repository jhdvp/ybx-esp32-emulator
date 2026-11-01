# 0093 — inkMOD v1.1.10 "starts but paints nothing": root cause, measured

Date: 2026-11-01. Lane: inkmod-cause2 (batch-6, Qoder). Branch
`agents/ybx-emu-b6-inkmod-cause2-20261101`, base HEAD `3a60072`.

## 0. Root-cause statement (testable, and tested)

> **inkMOD stops before painting because CPU1's sleep handshake waits on a
> register bit the emulator has no code path to ever set.** The guest spins in
> `rtc_sleep_wait_ready` loading `RTC_CNTL_INT_RAW` (0x60008044) from its
> literal pool; the shipped ESP32-S3 `rtc_cntl` model neither defines nor
> services that offset, so every read returns 0, `SLP_WAKEUP`/`SLP_REJECT` can
> never appear, and nothing downstream of the poll — including the SSD1677
> driver — is ever reached. The cross-core IPI is **not** the cause: it is
> modelled end to end and the shipped counters show it delivering.

The statement is falsifiable in one direction and was falsified in none: making
the polled value observable (by redirecting the same literal at an RTC offset
the model *does* service) takes the run from **0 panel commands / 423016 poll
visits** to **50 panel commands / 2 frames / 192000 RAM writes / 3848 non-white
bytes** with **1 poll visit**. That experiment is §7.

Consequence for the emulator lane (not done here — the brief forbids patching
QEMU): the gap is not "the panel model is wrong" but **the RTC sleep-wake source
is absent**: `0x60008040/44/48/4C` unimplemented (§3), the RTC device's `irq`
output never connected (§4), and no wake-source logic anywhere in `hw/` (§4).

## 1. Inputs, and how they were identified

Everything below ran on this host, in a throwaway container, against these
files. Checksums first, so a mismatch later is attributable:

```
$ shasum -a 256 ~/.local/state/ybx-emulator-upstream/qemu-wf-radio-20261031/build-ybx/qemu-system-xtensa \
    $ART/firmware/inkmod-v1.1.10-x4pro-flash-16m.bin \
    $ART/firmware/s3-flash-complete-rom-test.bin \
    $ART/roms/s3-full-rom-generator-test.bin \
    $ART/qa-20261029/ybx-test-sd-fat16-mbr-pristine.img
f65db0a7e6940a6c08ea2ffe4e8653227e8fbea70b25f4c695fc7a2de6a68236  qemu-system-xtensa
3fb63a3101fd662a3914c836a2c5ef4a571bb89a54eb1af4b1834326007258f0  inkmod-v1.1.10-x4pro-flash-16m.bin
206ed9661bd71477d1a54397c5a2cfe490179a3f59ab094f1e7421745c609523  s3-flash-complete-rom-test.bin
5b950be93a30610322a3b46049e4095cdda04f33f09fa13a9a9a6167b5994bd9  s3-full-rom-generator-test.bin
6c4c1bf4e6102e5963944dcf26572ab695c2259270e3aa9d0d0667787d8a7468  ybx-test-sd-fat16-mbr-pristine.img
```

`f65db0a7…` is the kernel named in the brief, so the runs are on the audited
26-patch chain. `$ART` =
`/Users/yaraslauhancharou/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029`.

Docker was available (`ybx-qemu-builder:20261029`, server linux/arm64), so
§5–§7 are measured rather than NOT MEASURED. One trap worth recording, because
the brief's literal string does not work:

```
$ docker run --rm --platform linux-arm64 alpine echo hi
docker: error parsing specified platform: "linux-arm64": unknown operating system or architecture: invalid argument
```

`--platform linux/arm64` is the valid form; the runbook in §9 emits it.

### 1.1 Files owned by this lane

* new `tools/ybx-system-emu/inkmod_root_cause.py` — image/segment walk, source
  coverage tables, hypothesis ranking, variant builder, container runbook.
* new `tools/ybx-system-emu/inkmod_forced_wake.py` — RTC read probe + the
  forced-wake experiment (drives the already-merged tracer as a subprocess; it
  does not modify it).
* new `tools/ybx-system-emu/test_inkmod_root_cause.py` — 61 host tests.
* new this report.

No QEMU source, no README/STATUS/experiments/README.md, and no other lane's
module was touched (`git status --porcelain` in §10).

## 2. Reproducing the signature on the current tree

Control run, inkMOD untouched, 60 s, exec trace on:

```
$ python3 /tools/probe_x4pro_inkmod_trace.py --qemu /qemu/build-ybx/qemu-system-xtensa \
    --rom /roms/s3-full-rom-generator-test.bin \
    --flash /fw/inkmod-v1.1.10-x4pro-flash-16m.bin \
    --sd /sd/ybx-test-sd-fat16-mbr-pristine.img \
    --label inkmod-pristine2 --profile inkmod --seconds 60 --exec-trace \
    --out /work/runs/inkmod-pristine2
```

(from `/tmp/ybx-0093/run-trio.sh`; full mount lines in §9)

`/tmp/ybx-0093/runs/inkmod-pristine2/report.json`:

```json
"verdict": {"rtc_int_raw": 0, "ipc_finish_cmd": 0, "samples": 241,
 "cpu0_tail": "ipc_isr_wait", "cpu1_tail": "rtc_sleep_start",
 "verdict": "deep_sleep_poll_stalled",
 "reason": "CPU1 polls RTC_CNTL_INT_RAW for SLP_WAKEUP/SLP_REJECT while it reads 0; CPU0 waits for the IPC ISR finish command; the SSD1677 has seen no traffic",
 "wedge_since_s": 4.508}
```

| measured | value |
|---|---|
| exec hits, `rtc_int_raw_poll` (0x4037cb2c) | **423016** |
| exec hits, `rtc_sleep_start` / `deep_sleep_wrapper` / `deep_branch_body` | 1 / 1 / 1 |
| exec hits, `esp_sleep_start` | 0 |
| CPU0 final PC | 0x4037a4a8 (`esp_ipc_isr_waiting_for_cmd`, 2 visits) |
| CPU1 final PC | 0x4037cb2c (the poll) |
| distinct CPU1 PCs across 241 samples | 5 |
| SSD1677 commands / frames / RAM writes / non-white | **0 / 0 / 0 / 0** |
| UART bytes after the bootloader banner | 225 (app prints nothing) |
| `RTC_CNTL_INT_RAW` (0x60008044) read | 0 |
| other RTC words the tracer samples (0x04,0x08,0x18,0x1c,0x20,0x3c,0x4c) | 0 |

So the previous lane's trace is confirmed on the current kernel, and the
premise of H1 is now measured inside the guest rather than inferred: the poll
target reads zero 423016 times and the guest never leaves that range.

The panel model is exonerated by a control on the same invocation — the factory
image, same binary, same ROM, same SD, 45 s
(`/tmp/ybx-0093/runs/factory-control2/report.json`):

```json
"verdict": {"verdict": "painted", "reason": "SSD1677 produced 4 frame(s)"},
"epd": {"frames": 4, "commands": 82, "ram-writes": 384000, "nonwhite": 4743,
        "busy-rise-edges": 4, "busy-fall-edges": 4, "busy-high-ms": 1400}
```

### 2.1 Trap: the vendor ROM boots nothing

The first pair of runs used `build-ybx/esp32s3_rev0_rom.bin` (5107c580353e…),
the image shipped next to the binary, because `-L` wants a file named
`esp32s3_rev0_rom.bin`. Both runs died identically before any app code:

```
$ head -6 /tmp/ybx-0093/runs/inkmod-pristine/uart0.log
ESP-ROM:esp32s3-20210327
Build:Mar 27 2021
rst:0x1 (POWERON),boot:0x4 (SPI_FLASH_BOOT)
Guru Meditation Error: Core 0 panic'ed (LoadProhibited)
Core 0 register dump:
PC      : 0x400494ff  PS      : 0x00060330  A0      : 0x8004a531  A1      : 0x3fceb690
```

The decisive detail is that the *whole UART log* is the same bytes for two
different firmwares:

```
$ shasum -a 256 /tmp/ybx-0093/runs/factory-control/uart0.log /tmp/ybx-0093/runs/inkmod-pristine/uart0.log
33e038bb00744791df107b5c208d5ba0b1d483a777e4ca6f35388436f5fe3f13  runs/factory-control/uart0.log
33e038bb00744791df107b5c208d5ba0b1d483a777e4ca6f35388436f5fe3f13  runs/inkmod-pristine/uart0.log
```

Same 824 bytes, same panic PC, for flash `206ed966…` and flash `3fb63a31…`: the
independent variable was the ROM, not the firmware. The accepted lanes
(`boot_matrix.py:62`, `panel_matrix.py:600`, experiment 0070's `run_guest.sh`)
stage the *generated complete* ROM `roms/s3-full-rom-generator-test.bin` under
that name. Any future inkMOD run must do the same or it measures nothing.

## 3. Missing behaviour #1 — RTC_CNTL register coverage, read from source

`python3 tools/ybx-system-emu/inkmod_root_cause.py analyze` walks
`include/hw/misc/esp32s3_rtc_cntl.h` (REG32 names) and
`hw/misc/esp32s3_rtc_cntl.c` (`esp32s3_rtc_cntl_read`/`_write` switch cases) and
classifies every offset the guest's sleep path touches. Output
(`/tmp/ybx-0093/cause.json`, `rtc_model_coverage.touched`):

```
offset meaning                                         defined  read   write  guest sees
0x00   OPTIONS0 (sleep/power-down config the guest wr) True     True   True   stored value
0x04   tracer-probed, unnamed in the tree              False    False  False  hardwired 0
0x08   tracer-probed, unnamed in the tree              False    False  False  hardwired 0
0x10   TIME0 (low half of the RTC counter)             True     True   False  stored value
0x14   TIME1 (high half of the RTC counter)            True     True   False  stored value
0x18   tracer-probed, unnamed in the tree              False    False  False  hardwired 0
0x1c   tracer-probed, unnamed in the tree              False    False  False  hardwired 0
0x20   tracer-probed, unnamed in the tree              False    False  False  hardwired 0
0x38   RESET_STATE (reset cause fields)                True     True   True   stored value
0x3c   tracer-probed, unnamed in the tree              False    False  False  hardwired 0
0x40   INT_ENA  (named from esp32c3_rtc_cntl.h)        False    False  False  hardwired 0
0x44   INT_RAW  (CPU1's poll target)                   False    False  False  hardwired 0
0x48   INT_ST   (named from esp32c3_rtc_cntl.h)        False    False  False  hardwired 0
0x4c   INT_CLR  (named from esp32c3_rtc_cntl.h)        False    False  False  hardwired 0
0x50   STORE0   (scratch register)                     True     True   True   stored value
0x54   STORE1   (scratch register)                     True     True   True   stored value

missing_int_group  ['0x60008040', '0x60008044', '0x60008048', '0x6000804c']
read_cases         ['A_RTC_CNTL_CLK_CONF', 'A_RTC_CNTL_OPTIONS0', 'A_RTC_CNTL_RESET_STATE',
                    'A_RTC_CNTL_STORE0', 'A_RTC_CNTL_STORE1', 'A_RTC_CNTL_STORE2',
                    'A_RTC_CNTL_STORE3', 'A_RTC_CNTL_STORE4', 'A_RTC_CNTL_STORE5',
                    'A_RTC_CNTL_STORE6', 'A_RTC_CNTL_STORE7', 'A_RTC_CNTL_SW_CPU_STALL',
                    'A_RTC_CNTL_TIME0', 'A_RTC_CNTL_TIME1', 'A_RTC_CNTL_TIME_UPDATE']
```

Answer to "which bits does the guest wait on": **`RTC_CNTL_INT_RAW`, offsets
0x40–0x4C as a group, are absent from the model.** The offset names come from
the sibling ESP32-C3 header in the same tree
(`include/hw/misc/esp32c3_rtc_cntl.h:157-199`: INT_ENA 0x40, INT_RAW 0x44,
INT_ST 0x48, INT_CLR 0x4C, `SLP_WAKEUP` bit 0, `SLP_REJECT` bit 1); the ESP32-S3
SoC header list is not on this disk, so the bit numbers are the C3's and are
flagged as such. The read switch has no `default:` logging branch for the
missing offsets, which is why the guest gets a silent 0 instead of a
`qemu_log_mask(LOG_UNIMP, …)` warning — the emulator never tells anyone it is
answering a register it does not have.

Attribution of the poll PC to the image (same `analyze` run,
`signature_attribution`): 6 of the 7 signature addresses land inside a loaded
segment; only `inkmod_ipc_finish_cmd` (0x3FCA290C) is absent, as expected for a
runtime BSS word. The literal the poll loads its address from:

```json
"forced_wake_plan": {"poll_pc": "0x4037cb2c", "unique": true,
 "candidates": [{"literal_vaddr": "0x40374b7c", "file_offset": "0x20d3fc",
                 "segment": 2, "region": "internal SRAM instruction (iram0)",
                 "reaches_poll_pc": true, "delta_bytes": -32688,
                 "replacement_word": "0x60008010"}]}
```

`unique: true` — across the whole 16 MiB image exactly one word holds
`0x60008044`, so redirecting it redirects every IRAM poll of INT_RAW.
`delta_bytes −32688` is inside the `l32r` reach window
`[-32768, +32764]` measured from `PC & ~3`, so the poll really does address
that pool word (arithmetic, not a decode: see §8).

## 4. Missing behaviour #2 — the wake source and its interrupt line

`analyze` output, `irq_wiring`:

```json
{"rtc_cntl_irq_initialised": true,
 "rtc_cntl_irq_connected_in_soc": [],
 "rtc_core_source_users": [],
 "rtc_source_can_raise_irq": false,
 "wakeup_token_in_rtc_model": [],
 "wakeup_token_in_soc": [],
 "sleep_field_in_rtc_header": ["esp32s3_rtc_cntl.h:30: ESP32_DEEPSLEEP_RESET = 5,"]}
```

Read commands behind those booleans:

```
$ cd ~/.local/state/ybx-emulator-upstream/qemu-wf-radio-20261031
$ grep -n "sysbus_init_irq(sbd, &s->irq)" hw/misc/esp32s3_rtc_cntl.c
199:    sysbus_init_irq(sbd, &s->irq);
$ grep -rn "ETS_RTC_CORE_INTR_SOURCE" hw/ | wc -l
0
$ grep -rniE "SLP_WAKEUP|SLP_REJECT|EXT_WAKEUP|wakeup_cause" hw/ | wc -l
0
$ grep -c "rtc_cntl" hw/xtensa/esp32s3.c
11
```

So: **the interrupt the guest is waiting for has no producer and no wire.** The
only "sleep" token in the RTC header is a *reset cause* enum
(`ESP32_DEEPSLEEP_RESET = 5`), and the only `deep_sleep` state in the tree is
the SSD1677 panel model's own flag — i.e. the panel knows about sleep, the RTC
does not. This is H2 and it is what turns H1 from "one register is missing" into
"a subsystem is missing": implementing `0x44` as a stored register would still
leave nothing that sets it.

## 5. Is it the cross-core IPI? Source and runtime both say no

Source (`analyze`, `ipi_chain`) — every stage of the FROM_CPU path exists, and
the counters that let a runtime check decide it ship already:

```json
{"system_from_cpu_registers_modelled": true,
 "from_cpu_level_cleared_on_zero_write": true,
 "clock_lines_wired_to_matrix_inputs": true,
 "matrix_routes_and_counts": true,
 "matrix_reset_default_is_unrouted": true,
 "cpu_level_entry_in_pic": true,
 "counters_exported_for_runtime_check": ["ybx-cpu-intr-{0..3}-sets",
  "ybx-cpu-intr-{0..3}-clears", "ybx-cpu-intr-levels", "ybx-map-writes",
  "ybx-last-map-addr", "ybx-last-map-value", "ybx-cpu{0,1}-asserted",
  "ybx-cpu{0,1}-unrouted", "ybx-cpu{0,1}-from-cpu-asserted",
  "ybx-cpu{0,1}-from-cpu-default-route", "ybx-cpu{0,1}-from-cpu-int"]}
```

Runtime, on the *wedged* guest (`inkmod-pristine2/report.json`):

```json
"clock":     {"ybx-cpu-intr-0-sets": 5, "ybx-cpu-intr-1-sets": 181,
              "ybx-cpu-intr-2-sets": 1, "ybx-cpu-intr-3-sets": 0}
"intmatrix": {"ybx-map-writes": 218, "ybx-last-map-addr": 2168, "ybx-last-map-value": 3,
              "ybx-cpu0-from-cpu-asserted": 187, "ybx-cpu0-from-cpu-int": 28,
              "ybx-cpu0-from-cpu-default-route": 181,
              "ybx-cpu1-from-cpu-asserted": 187, "ybx-cpu1-from-cpu-int": 6,
              "ybx-cpu1-from-cpu-default-route": 6}
```

187 FROM_CPU assertions reached the matrix, only 6 were dropped at the reset
default route, and 6 became a real interrupt on CPU1 — the lane works. CPU0's
181 "default-route" hits are for a source IDF routes to core 1 only, so they
are expected, not a bug. For comparison, the factory run that *paints* shows the
same counters an order of magnitude busier (`ybx-cpu0-from-cpu-asserted` 4001,
`map-writes` 220), so a non-zero value here means traffic, not a stuck probe.
CPU0's `esp_ipc_isr_waiting_for_finish_cmd` is therefore downstream: it is
waiting on work that CPU1 never gets to do, because CPU1 is in the poll of §3.

## 6. Two integrity guards stand between a byte edit and the sleep path

To force the wake without patching QEMU, one word in the image must change. That
is where the two traps are, and both were hit and measured before being solved.

**Guard 1 — XOR checksum byte.** The first variant changed only the literal and
the bootloader rejected it (`/tmp/ybx-0093/runs/inkmod-forced-wake2/uart0.log`):

```
E (701) esp_image: Checksum failed. Calculated 0x4f read 0x33
```

That single line pins the model: the stored byte lives at 0x44912f (the last
byte of the 16-byte block holding the segment-data end, walk_end 0x449128) and
the guest's computed value is exactly `0x33 ^ 0x44 ^ 0x38 = 0x4f`, i.e. the XOR
of the image with one byte changed. `checksum_position()` encodes that rule and
a test asserts the guest's own numbers
(`test_matches_the_value_the_guest_itself_reported`).

**Guard 2 — appended SHA-256.** With the checksum repaired, the same image got
past guard 1 and died at the next one
(`/tmp/ybx-0093/runs/inkmod-forced-wake3/uart0.log`, 31 occurrences):

```
E (701) esp_image: Image hash failed - image is corrupt
E (744) boot: OTA app partition slot 0 is not bootable
E (756) esp_image: image at 0x650000 has invalid magic byte (nothing flashed here?)
E (766) boot: No bootable app partitions in the partition table
```

32 high-entropy bytes sit at 0x449130..0x44914f, followed by erased `0xff`. The
region rule was not available from any IDF source on this disk, so it was
derived by discrimination and validated by reproduction:

```
$ python3 - <<'PY'
...  hashlib.sha256 over candidate regions, compared to d[0x449130:0x449150]
True   contiguous [0x10000,0x449130)
False  skip header byte 16
False  up to walk_end 0x449128
stored   74ab4a062d3aae72629f73ecb46b02839663c258eea6b16a2a3c832b142d403b
computed 74ab4a062d3aae72629f73ecb46b02839663c258eea6b16a2a3c832b142d403b
```

Plain SHA-256 over `[app_offset, hash_position)` — no skipped header field, and
it ends where the digest begins. Because the rule reproduces the untouched
image's digest byte for byte, re-applying it to the patched image is provably
correct, and `build_variant` now repairs the literal, the checksum byte and the
digest in one pass — and refuses to write anything if a digest is present but
the rule does not explain it (`test_variant_refuses_a_digest_the_rule_does_not_explain`).

## 7. The experiment: make the wake bit observable

`inkmod_forced_wake.py experiment` does three things in one container: probes
what the running guest actually reads at each *modelled* RTC offset, picks the
substitution target from that measurement, builds the image variant, and runs it
through the merged tracer against the §2 control.

Probe pass (pristine image, 25 s settle, 3 rounds 2 s apart) —
`/tmp/ybx-0093/five/probe.json`:

| offset | register | values read by the guest | changing | wake bits (0/1) |
|---|---|---|---|---|
| 0x10 | TIME0 | 501501, 501501, 501501 | no | **yes** (0x7A6FD is odd) |
| 0x14 | TIME1 | 0, 0, 0 | no | no |
| 0x38 | RESET_STATE | 12353, 12353, 12353 | no | yes (0x3041) |
| 0x50 | STORE0 | 0, 0, 0 | no | no |
| — | poll literal @ 0x40374b7c | `0x60008044` ×3 | — | (read back with `xp`, so IRAM is readable through the system AS) |

Side note for the sleep-wake lane: three samples 2 s apart give **the identical
value** — the modelled RTC counter does not tick. Also captured: at probe time
CPU1 `PC 0x4037cb2c` → signature `rtc_sleep_start`, CPU0 `0x4037a4a8` →
`ipc_isr_wait`, i.e. the probe reproduced the wedge before changing anything.

`choose_substitute()` prefers a changing value, then one with the wake bits set,
then any nonzero value; here it selected **TIME0 (0x60008010)**. Variant record:

```json
{"literal_vaddr": "0x40374b7c", "file_offset": "0x20d3fc",
 "word_before": "0x60008044", "word_after": "0x60008010",
 "checksum_position": "0x44912f", "checksum_before": "0x33", "checksum_after": "0x67",
 "app_hash": {"state": "repaired", "position": "0x449130"},
 "bytes_changed": 34,
 "variant_sha256": "592110296b30c2432d751c5551963681b47dc414c55c709424f9736cd8db31dd"}
```

(34 = the one differing literal byte + the checksum byte + 32 digest bytes; the
image is otherwise bit-identical, `input_sha256 3fb63a31…`.)

Result — the decision rule from the runbook, executed:

| | control (pristine) | **forced-wake (poll → TIME0)** | factory (reference) |
|---|---|---|---|
| verdict | `deep_sleep_poll_stalled` | `guest_panic` | `painted` |
| `rtc_int_raw_poll` exec hits | 423016 | **1** | n/a |
| SSD1677 commands | 0 | **50** | 82 |
| SSD1677 frames | 0 | **2** | 4 |
| panel RAM writes | 0 | **192000** | 384000 |
| non-white bytes | 0 | **3848** | 4743 |
| busy rise/fall edges | 0/0 | **3/3** (1050 ms high) | 4/4 |
| distinct CPU1 PCs | 5 | **17** | — |
| UART bytes | 225 | 194590 | 225 |

**H1 CONFIRMED.** The poll released on its first visit (423016 → 1) and the
panel then received real traffic: 50 commands, 2 frames, 3848 non-white bytes.
First panel activity is at `t = 5.502 s` with `epd_commands 24, epd_frames 1` —
inside the *second* boot, after the guest got past the sleep path once:

```
E (1613) sleep: Deep sleep request is rejected
abort() was called at PC 0x40377508 on core 1
Backtrace: 0x4037e7ed:0x3fcedad0 0x40375645:0x3fcedaf0 0x4037e7b5:0x3fcedb10
           0x40385d3d:0x3fcedb30 0x40377508:0x3fcedbb0 0x4200c10a:0x3fcedbd0 ...
ELF file SHA256: ccf8eff5f
Rebooting...
```

That is the expected shape: the substituted value (501501 = 0x7A6FD) satisfies
the mask, IDF's post-poll logic classifies the event as a **reject**, and
`esp_deep_sleep_start` aborts because a rejected deep sleep is a fatal firmware
condition on real hardware. The run then reboots, and the rebooted guest is the
one that paints — which is exactly what H1 predicts for the *paint* path: once
the sleep handshake is not the binding constraint, inkMOD reaches the driver.
The abort is a second, separate gap (`sleep-entries 0`, `deep-sleep 0`: the
panel never entered sleep in this run), and its own fix is out of scope here.

Corroborating write-side evidence that the guest genuinely ran a full boot
sequence twice: the run copy of flash changed in blocks
`[57344, 16711680, 16715776, 16719872, 16723968]` — 0xE000 is the `otadata`
partition (boot-count/slot), 0xFF0000 is `coredump` (the abort) — and the guest
printed 1890 `i2cWriteReadNonStop` lines (945 with `ESP_ERR_INVALID_STATE`),
i.e. it reached its I2C touch polling, which the wedged control never does.
`inkmod_config.wakeup_triggers` read 2 (`EXT1` armed) in the control and 0 in
the forced run, sampled before the app had re-initialised its config after the
reboot; the 2 in the control is the number that matters for H6.

## 8. Ranked hypotheses, with the measurement each one needed

Statuses are what this session measured, not what the source suggests.

| # | hypothesis | falsifying measurement | result |
|---|---|---|---|
| **H1** | CPU1 waits on `RTC_CNTL_INT_RAW`, an offset the model leaves at 0, so the loop never exits and nothing downstream runs | redirect that literal at a *modelled* RTC offset that reads nonzero; REFUTED if CPU1 stays in 0x4037cab8..0x4037cb60 and `ybx-epd-commands` stays 0 | **CONFIRMED** (§7: 1 poll hit, 50 commands, 2 frames) |
| **H2** | Even a correct INT_RAW could not be raised: the device `irq` is initialised but unconnected, `ETS_RTC_CORE_INTR_SOURCE` has no users | `grep -rn ETS_RTC_CORE_INTR_SOURCE hw/` yields a wired input, or the RTC gains an interrupt property | **CONFIRMED as a co-required gap** (§4: 0 users, 0 connections, 0 wake tokens in `hw/`) |
| **H3** | CPU0's `esp_ipc_isr_waiting_for_finish_cmd` is a lost cross-core IPI | read `ybx-cpu-intr-{2,3}-sets`, `ybx-cpu{0,1}-from-cpu-asserted/-default-route/-int` on the wedged guest | **REFUTED as cause** (§5: 187 asserted, 6 default-route, 6 delivered; chain complete in source) |
| **H4** | INT matrix addressing is off by a block, so an ack write silently rewrites a routing entry | compare `ybx-last-map-addr` / `ybx-map-writes` against the addresses `esp_rom_route_intr_matrix` uses | **NOT MEASURED** — numbers recorded (last addr 2168 = 0x878, writes 218 vs 2112/0x840 and 220 in the painting control), but no IDF headers or ELF on this disk to say which addresses are correct |
| **H5** | The SSD1677 model is at fault; the paint code ran and produced nothing | forced-wake run: commands > 0 refutes it | **REFUTED as cause** (§7: same panel model, same machine flags, 50 commands / 2 frames once the guest arrives) |
| **H6** | inkMOD armed no wake source (`wakeup_triggers == 0`), so the emulator is faithfully showing a firmware bug | read `inkmod_config.wakeup_triggers` from a wedged run | **REFUTED** (`/tmp/ybx-0093/runs/inkmod-pristine2/report.json`: `"wakeup_triggers": 2`, EXT1 — a source this model cannot honour) |

## 9. Reproduction (the runbook this lane emits, and ran)

`inkmod_root_cause.py runbook` prints this contract; `/tmp/ybx-0093/run-forced4.sh`
is the file actually executed (§7):

```bash
docker run --rm --platform linux/arm64 --network none \
  -v $TREE:/qemu:ro -v $REPO/tools/ybx-system-emu:/tools:ro \
  -v $ART/firmware:/fw:ro -v $ART/qa-20261029:/sd:ro \
  -v $ART/roms:/roms:ro -v /tmp/ybx-0093:/work \
  ybx-qemu-builder:20261029 bash -lc '
    python3 /tools/inkmod_forced_wake.py experiment \
      --qemu /qemu/build-ybx/qemu-system-xtensa \
      --rom /roms/s3-full-rom-generator-test.bin \
      --flash /fw/inkmod-v1.1.10-x4pro-flash-16m.bin \
      --sd /sd/ybx-test-sd-fat16-mbr-pristine.img \
      --control /work/runs/inkmod-pristine2/report.json \
      --settle 25 --seconds 60 --out /work/five'
```

Only `/work` is writable; the shared build tree, this repo's tools directory,
the firmware folder, the SD image and the ROM are all `:ro`; the emulator binary
is never rebuilt; the container is `--rm` and `--network none`; the live
browser container is never referenced. Determinism: the two inkMOD runs differ
in exactly one input, the flash image (`3fb63a31…` vs `59211029…`).

## 10. Tests, and what is still NOT MEASURED

```
$ python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q
Ran 876 tests in 31.993s
OK (skipped=11)

$ python3 -m unittest test_inkmod_root_cause -q     # this lane's 61 tests
Ran 61 tests in 0.607s
OK
```

NOT MEASURED, with the reason each one is out of reach from this disk:

* **Symbol names.** No inkMOD ELF/map is present, so `rtc_sleep_wait_ready`,
  `esp_ipc_isr_waiting_for_finish_cmd` and `0x40377508` are the previous lane's
  labels. What this session measured is their *addresses* and the fact that
  they sit in loaded IRAM segments (`signature_attribution`: 6/7).
* **`l32r` decoding.** No Xtensa disassembler is available, so the
  poll→literal link is arithmetic (`delta_bytes −32688` inside
  `[-32768, +32764]` from `PC & ~3`) plus the runtime read-back of the literal
  (`xp` returned `0x60008044`), not a decoded instruction.
* **The S3 wake bit positions.** Taken from the ESP32-C3 header in the same
  tree; the S3 SoC header is absent. The experiment is mask-agnostic on purpose
  (it selects a value with bits 0 and 1 set), so this does not weaken §7, but
  the "reject" classification in the guest log is consistent with *some* bit in
  0x7A6FD, not a proven `SLP_REJECT`.
* **GDB-level `INTSET`/`INTENABLE`.** No GDB session was run; the IPI verdict
  rests on the shipped QOM counters plus source.
* **H4's matrix geometry** (§8) and **the `abort()` at 0x40377508**: both
  observed, neither explained. The abort path needs its own experiment.
* **IDF's own hash-region definition.** The rule in §6 was derived by
  reproducing the image's stored digest; it was not read from IDF source, and
  the earlier Δ0x5c mismatch when hashing from 0x10018 remains unreconciled.
* **Whether inkMOD 1.1.10 paints on real hardware with a *proper* wake.** Only
  the emulator-side gate was tested.
