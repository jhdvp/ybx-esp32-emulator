# 0081 — What the Wi-Fi stub actually intercepts in the early boot (2026-10-31)

## Claim under test (from the batch brief)

> With `-machine …,ybx-x4pro-wifi-stub=on` stock CrossPoint 2.0.0rc prints only
> ROM lines and hangs earlier; with the stub off it prints two
> `Preferences nvs_open failed: NOT_FOUND` lines and hangs later.

## Verdict — measured, not inferred

1. **The claimed split does not reproduce.** With the stub ON, CrossPoint
   prints the same two `Preferences … NOT_FOUND` lines as with the stub OFF.
   Every transcript pair below is byte-identical modulo the `[  ms]` field;
   the factory image's UART is byte-identical across configs (SHA-256
   `477f6433b47482262573dbe2b27722e077b0792808deb85d61f9d34ea9cf557a`).
2. **There is no addressing difference to find.** The set of registers the
   guest touches with the stub OFF, and the set the stub intercepts with it ON,
   overlap in exactly **one word: `0x60006090`** (FE window, `FE+0x90`). Both
   paths return the same value (`0`), and the guest's single write there is
   `= 0x00000000`, so the stub's storage stays `0`. The difference in when the
   `Preferences` line appears is **timing jitter, not addressing** — evidence
   below.
3. **The "only ROM lines" signature in the claim matches inkMOD v1.1.10, not
   the stub.** inkMOD prints exactly the ROM banner + `entry 0x403c8898` and
   stops, with the stub ON *and* OFF (project-verified fact; my own
   `inkmod-on-tap` run reproduces the 225-byte ROM-only transcript). The claim
   was very likely a firmware mix-up or a stale capture. I could not inspect
   the original capture behind the claim — it is not in the artifacts set
   (missing artifact: the UART log / kernel SHA the claimant measured).

## How the measurement was done

Two independent enumeration methods, run in **my own throwaway containers**
(`docker run --platform linux/arm64 --network none ybx-qemu-builder:20261029`,
helper `/private/run_ab.sh`; the live `ybx-x4pro-browser-8784` was never
touched). Guest: `crosspoint-2.0.0rc-x4pro-flash-16m.bin` and factory
`s3-flash-complete-rom-test.bin`, machine flags copied from
`serve_x4pro_browser.py:498-517` (`ybx-x4pro-i2c/gt911/sd-power/spi2/epd=on`).

### Method A — runtime tap against the live kernel (`f65db0a7…`)

`-global driver=misc.esp32s3.wifi_stub,property=x-radio-tap,value=on` logs the
first touch of every stub-owned word (whole-run per-address dedup, so a
read+write pair at one address logs once — that is why only the `read` line
appears; the `write 0x60006090 = 0` is visible via Method B).

```
$ … qemu-system-xtensa(…f65db0a7…) -M esp32s3,…,ybx-x4pro-wifi-stub=on \
    -global driver=misc.esp32s3.wifi_stub,property=x-radio-tap,value=on  (90 s)
CrossPoint:  warning: [radio-stub] read 0x60006090 = 0x00000000 (size 4)   # the only tap line
Factory:     warning: [radio-stub] read 0x60006090 = 0x00000000 (size 4)   # the only tap line
```

Across the whole 90-120 s window **no modem-cluster word** (`0x60031000-
0x60035FFF`) was ever touched by either guest — those five windows sit
completely unused at boot; the stub only "answers" the FE word.

### Method B — enumerate everything with the stub OFF

The stub-off side had no observability: unmodeled reads silently return 0 from
the catch-all `esp32s3.iomem` (`hw/xtensa/esp32s3.c:119
#define ESP32S3_IO_WARNING 0`, warn block `:463`, silent handlers
`esp32s3_io_read`/`esp32s3_io_write` at `:536`/`:557` of
`qemu-wf-radio-20261031`). Fix: in a **private `cp -a` copy**
(`/private/qemu-0081-wifi-ab`) I (a) applied the narrowing patch below and
(b) flipped `ESP32S3_IO_WARNING` to `1` — a diagnostic-only edit that is
**excluded from the shipped patch**. Rebuilt binary:
`97cf825ad32b9ffa35f51addd474fc4f3aa40cb2897638a1e3b88a851a552f2f`.

Every catch-all access then became a stderr line
(`[ESP32-S3] Unsupported read to $…` / `Unsupported write $… = …`). Distinct
addresses touched in 90 s:

| run | stub | distinct catch-all addrs | Δ vs off |
|---|---|---|---|
| `warn-off-cross`  | off | 119 | — |
| `warn-on-cross`   | on (narrowed) | 118 | **−{0x60006090}** |
| `warn-off-factory`| off | 119 | — |
| `warn-on-factory` | on (narrowed) | 118 | **−{0x60006090}** |

In both firmwares the *only* address absorbed by the stub is `0x60006090`, and
**zero** addresses moved in the opposite direction (nothing the stub newly
exposed to the catch-all). Full lists:
`/private/runs/warn-off-cross/distinct-off.txt`,
`/private/runs/warn-on-cross/distinct-on.txt`.

The boot-time sequence around the overlap word (`warn-off-cross/qemu.log`):

```
3400: warning: [ESP32-S3] Unsupported read to $60006090, size = 4
3402: warning: [ESP32-S3] Unsupported write $60006090 = 00000000
```

Read → write-zero → (never read again). Catch-all read returns 0; stubbed
read returns storage, which is 0 before the write and stays 0 after it. The
guest cannot observe any difference at this word.

### Who reads `0x60006090`

The factory ELF (l32r literal-pool scan, `xtensa-esp32s3-elf-objdump`) has the
literal `0x60006090` at:

- `0x40381fa2` inside **`rtc_sleep_pu`** (IDF `rtc_sleep.c:43`; the same
  function performs the single observed FE2 touch `0x600050f0` — see the
  FE2 row of `FACTORY_BASELINE` in `wifi_radio_inventory.py:288-293`, and
  note `0x600050f0` really appears in my stub-off lists of *both* images);
- `0x4226e44f` / `0x4226e50f` in the **`phy_freq_correct`** region (0060
  inventory lists `phy_freq_correct` as an FE/NRX/BB toucher).

So this is the always-running PHY/sleep bring-up path, executed before the
Arduino `Preferences` init, in every firmware — which is why the tap line is
identical for CrossPoint and the factory image.

## Why the claimed "earlier hang" is timing, not addressing

`Preferences.cpp:47` timestamps across my runs (same host, same container
template, ±90 s):

| run | config | line 1 | line 2 |
|---|---|---|---|
| stub-off | live f65db0a7 | 6859 ms | 6868 ms |
| stub-on-tap | live f65db0a7 + tap | 6500 ms | 6505 ms |
| crosspoint-on-8s | live, stub on | 6455 ms | 6470 ms |
| crosspoint-on-long | live, stub on | 8085 ms | 8247 ms |
| warn-off-cross | narrowed+warn, off | 3219 ms | 3224 ms |
| warn-on-cross | narrowed+warn, on+tap | 2794 ms | 2799 ms |

Stub-on runs span 6455-8085 ms vs stub-off 6859 ms — the distributions
overlap and shift *earlier* as often as later; toggling stderr logging
(`ESP32S3_IO_WARNING=1`) moves both configs by ~2×, which dwarfs any
stub effect. A single-register intercept that returns an identical `0`
cannot produce the claimed ROM-only outcome; the ROM-only outcome belongs
to inkMOD (see Verdict 3).

## The narrowing patch — `0081-wifi-stub-overlap.patch`

The 0071 Stage-1 stub claimed full 4 KiB windows
(`hw/misc/esp32s3_wifi_stub.c:28-33` of `qemu-wf-radio-20261031`: e.g.
`tx_crypto 0x60034000+0x1000`, `wdev_high 0x60035080+0xF80`, `fe
0x60006000+0x1000`). The measured inventory extremes
(`wifi_radio_inventory.py:212-293`) are much smaller, and 0x60035E00-
0x60035EFF (old `wdev_high` area containing the host stats block) was never
justified as radio space at all. The patch shrinks each region to
`observed_max + 4` word-aligned and moves the stats block to its own region:

```c
{ "modem_coex", 0x60031000, 0x0534 },  /* to 0x60031530 */
{ "wlan_mac",   0x60033000, 0x0D18 },  /* to 0x60033d14 */
{ "tx_crypto",  0x600342f8, 0x0130 },  /* to 0x60034424 */
{ "wdev_low",   0x60035000, 0x007C },  /* stops before the TRNG word */
{ "wdev_high",  0x60035080, 0x00A0 },  /* to 0x6003511c */
{ "fe",         0x60006000, 0x01E8 },  /* to 0x600061e4; tap 0x60006090 */
{ "ybx_stats",  0x60035f00, 0x0020 },  /* host ABI, not radio */
```

- `ESP32S3_WIFI_STUB_WINDOW_COUNT` stays **6** (radio count pinned by
  `probe_x4pro_wifi_radio_qtest.py:54` — `stats.window_count == 6`), a new
  `REGION_COUNT 7` drives the mapping loops (`esp32s3.c:1048`).
- The TRNG word `0x6003507C` (`include/hw/misc/esp32s3.rng`) stays carved out
  between `wdev_low`/`wdev_high`; the stats special-case in the device ops
  keys off the absolute address, so it works from the new region.
- Nothing else changes: no IRQ, no netdev, property still default-off
  (pinned by the honesty tests).

Anything outside a measured range now falls back to `esp32s3.iomem`
zero-fill, so the stub can never answer a register the inventory never
proved — including the entire early-boot path except the single FE word,
which is legitimately in-range (`0x60006000-0x600061e4`) and is kept.

Validation performed:

```
$ git -C /Users/yaraslauhancharou/.local/state/ybx-emulator-upstream/qemu-wf-radio-20261031 \
    apply --check tools/ybx-system-emu/experiments/0081-wifi-stub-overlap.patch
APPLY_CHECK_OK against live tree            # read-only check; tree untouched
$ <build of the private copy>               # ninja succeeded, 97cf825a…
$ run_ab.sh warn-on-cross ",ybx-x4pro-wifi-stub=on" …  # guest equivalence above
```

## Host tests — `test_wifi_stub_side_effect.py`

Pure-Python, stdlib-only, <20 s, no Docker/QEMU/guest: reconstructs each
patched file from `0081-wifi-stub-overlap.patch` with a mini unified-diff
applier and checks the table against the pinned `FACTORY_BASELINE` and the
qtest probe's `STUB_OPS`. 11 tests:

- region shape = the 6 measured windows + `ybx_stats`; each window tight
  around its inventory range (slack < 4 bytes); `tx_crypto` starts at the
  measured `0x600342f8`, not `0x60034000`;
- `0x60006090` is covered only by `fe` (the measured tap word);
- RNG word `0x6003507C` never claimed; every `STUB_OPS` probe address still
  inside a region; stats no longer hides inside `wdev_high`;
- never-stubbed inventory windows (FE2/BT_BB/NRX/BB/SYSCON/RTCCNTL) stay
  catch-all; the old wide claim areas (`0x60031534+`, `0x60033D18+`,
  `0x60034428+`, `0x60034000-0x600342F7`, `0x60035120-0x60035EFF` radio part,
  `0x600061E8+`) now fall back to zero-fill;
- honesty boundaries: patch adds no `sysbus_connect_irq`/`qemu_new_net_client`/
  default-on, `meson.build` untouched, machine diff line contains only
  `REGION_COUNT`.

```
$ cd tools/ybx-system-emu && python3 -m unittest test_wifi_stub_side_effect
Ran 11 tests in 0.001s — OK
```

## What stays unproven

- The orchestrator's original capture for the claim (UART log + kernel SHA
  behind "only ROM lines with stub on") — not among the artifacts; I can
  only show the signature matches inkMOD and is not reproducible on
  `f65db0a7…`.
- Whether some *other* guest (not CrossPoint/factory/inkMOD) would touch a
  modem window at boot — the inventory's 692 l32r sites are compile-time,
  not runtime, facts.
- The qtest probe itself was not re-executed against the narrowed build
  (this build config lacks the qtest harness wiring used in 0071); the host
  tests pin the probe's table expectations statically instead.
- Ordering detail: the tap dedup hides the write's timestamp at
  `0x60006090`; Method B shows read-then-write-zero but not the guest-side
  code path timing between them.

## Reproduction commands

Container template (`/private/run_ab.sh`, image `ybx-qemu-builder:20261029`,
`--network none`, ROM seed `s3-full-rom-generator-test.bin`):

```
docker run --rm --platform linux/arm64 --network none \
  -v $QEMU_TREE:/qemu:ro -v $QA:/work:ro -v $FW:/fw:ro -v $OUT:/out:rw \
  ybx-qemu-builder:20261029 bash -c 'timeout 90 \
    /qemu/build-ybx/qemu-system-xtensa -machine esp32s3,ybx-x4pro-i2c=on,\
ybx-x4pro-gt911=on,ybx-x4pro-sd-power=on,ybx-x4pro-spi2=on,ybx-x4pro-epd=on\
[,ybx-x4pro-wifi-stub=on] [-global driver=misc.esp32s3.wifi_stub,\
property=x-radio-tap,value=on] -m 8M -global ssi_psram.is_octal=on -nographic \
    -monitor none -serial file:$d/uart0.log -serial null -serial null -serial null \
    -L $d/rom -drive file=$d/flash.bin,if=mtd,format=raw \
    -drive file=$d/sd.img,if=sd,format=raw'
```

Run matrix used: `stub-off`, `stub-on-tap`, `crosspoint-on-8s`,
`crosspoint-on-long`, `factory-off`, `factory-on-tap`, `inkmod-on-tap`
against the live binary `f65db0a7…`; `warn-off-cross`, `warn-on-cross`,
`warn-off-factory`, `warn-on-factory` against the narrowed+diagnostic
private binary `97cf825a…`. Raw logs:
`/Users/yaraslauhancharou/.local/state/xteink-agent-workspaces/ybx-emu-batch5-20261031/private/runs/<name>/`.
