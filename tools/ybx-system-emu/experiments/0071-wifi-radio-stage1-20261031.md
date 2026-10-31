# 0071 — Wi-Fi radio Stage 1: the register stub is real, compiled and qtest-verified (batch 4, wifi-radio lane)

Date: 2026-10-31. This is the first time the 0060 proposal has been **built
and executed**. Everything below was measured in my own isolated QEMU copy
(`/Users/yaraslauhancharou/.local/state/xteink-agent-workspaces/ybx-emu-batch4-20261031/qemu-0071-wifi-radio-stage1`,
`cp -a` of the pinned checkout at HEAD `febae182e132e4055529be423a818225ebddaa3a`
plus its uncommitted patch state). The pinned checkout, the live container
`ybx-x4pro-browser-8784` and the shared build tree were not modified. No
guest was booted here; the CPU-frozen qtest machine only.

## What changed

| File | Role |
| --- | --- |
| `tools/ybx-system-emu/experiments/0071-s3-wifi-radio-stage1.patch` | The Stage-1 device, 416 lines, 4 files. SHA-256 `ba032d4edfd59d68633716d250ccf3aacf20e3bdd1b29d79fb9d7f09e2352e62`. Verified twice read-only: `git apply --check` → exit 0 against the pinned checkout in its current state. |
| `tools/ybx-system-emu/test_wifi_radio_stage1.py` | 14 host tests (0.086 s, no Docker/guest): a pure-Python reference model built from the tables parsed out of the patch, replaying the probe's `STUB_OPS` sequence, plus the honesty-boundary checks (default-off, RNG carve-out, conditional init, no IRQ/netdev). |
| this file | measurements and limits. |

Inside the patch (line → meaning):

- `…0071-s3-wifi-radio-stage1.patch:33-39` — six windows exactly over the
  pinned 0060 inventory clusters: `modem_coex 0x60031000/0x1000`,
  `wlan_mac 0x60033000/0x1000`, `tx_crypto 0x60034000/0x1000`,
  `wdev_low 0x60035000/0x007C`, `wdev_high 0x60035080/0x0F80`,
  `fe 0x60006000/0x1000`. WDEV is split **around** `ESP32S3_RNG_BASE
  0x6003507C` (`include/hw/misc/esp32s3_rng.h:26`), so `esp_random` keeps
  the real TRNG word.
- `:60-65` — the four write-ack-filtered ranges (forced zero on read, counted
  but never latched on write): `0x60033c3c-0x60033c44` (MAC interrupt
  get/clear), `0x60033ca4-0x60033cb0` (TXQ state), `0x6003500c-0x60035020`
  (TSF get/set — **frozen**, never ticks), `0x60035118-0x60035120`
  (power-interrupt get/clear).
- `:91-129 / :131-155` — read/write paths: ordinary words are register
  storage with 1/2/4-byte lanes; stats reads are excluded from the guest
  counters.
- `:104-112 + :396-398` — read-only stats block at `0x60035f00` (host
  observability; the highest measured factory WDEV literal is `0x6003511c`):
  magic `0x59425230`, window count, guest-visible read/write counters, last
  read/write addresses.
- `:204` — device property `x-radio-tap` (warn_report on the first touch of
  each absolute address), default off.
- `:215` — `dc->user_creatable = false`; `:375` — type
  `misc.esp32s3.wifi_stub`.
- `:281-282` — machine property `ybx-x4pro-wifi-stub` **defaults false**;
  `:303-318` — the whole device (child init, realize, six
  `memory_region_add_subregion_overlap(sys_mem, …, 0)` maps) is guarded by
  `if (ss->ybx_x4pro_wifi_stub)`. Off ⇒ the machine is byte-identical to the
  shipping emulator.
- `:240` — one meson line adds `esp32s3_wifi_stub.c` to
  `CONFIG_XTENSA_ESP32S3`.

After application, in the built tree: `hw/misc/esp32s3_wifi_stub.c:27/54/85/125/198`,
`hw/xtensa/esp32s3.c:662, 1023-1036, 1322-1327`,
`include/hw/misc/esp32s3_wifi_stub.h:22, 43-47`.

## Stage 1 found and fixed a real defect in the 0060 proposal

0060 initialised the child object unconditionally (`object_initialize_child`
next to `rng`) but only realised it when the property was on. With property
OFF — the shipping configuration — QEMU 9.2's post-`machine_init` check aborts
every `esp32s3` run (first probe attempt, real output):

```text
qemu-system-xtensa: ../hw/core/qdev.c:300: qdev_assert_realized_properly_cb: Assertion `dev->realized' failed.
RC=134
```

Fix (patch `:303-305`): `object_initialize_child` moved **inside** the
guarded block. `test_wifi_radio_stage1.py::test_machine_property_defaults_off_and_guards_everything`
pins that the init appears exactly once and only after the guard — a replay
of the 0060 patch through these tests fails. This is exactly the class of
defect that only compile-and-run can find; `git apply --check` could not.

## Build evidence (isolated copy, real output)

```sh
docker run --rm --platform linux/arm64 \
  -v "$PWD/qemu-0071-wifi-radio-stage1:/src" -w /src/build-ybx \
  ybx-qemu-builder:20261029 ninja -j2 qemu-system-xtensa
```

```text
[4/6] Compiling C object libcommon.a.p/hw_misc_esp32s3_wifi_stub.c.o
[5/6] Compiling C object libqemu-xtensa-softmmu.a.p/hw_xtensa_esp32s3.c.o
[6/6] Linking target qemu-system-xtensa
```
(no warnings; after the guard fix: `[4/4] Linking target qemu-system-xtensa`)

Rebuilt binary SHA-256
`8501e824e3aa6ed942ceda734a1278b740a3075b7bdcaf0bd149eaf783f7c3c0`
(recorded by the probe; the shared tree's binary is untouched).

## qtest evidence — probe_x4pro_wifi_radio_qtest.py against the rebuilt binary

```sh
python3 tools/ybx-system-emu/probe_x4pro_wifi_radio_qtest.py \
  --qemu-root …/qemu-0071-wifi-radio-stage1 --with-stub \
  --out /tmp/ybx-wifi-radio-0071/capability-0071.json
```

Default machine (`-M esp32s3`, property off): `radio_register_windows_unmodelled:
true`, `reads_all_as_expected: true`, WDEV still decoded only by
`misc.esp32s3.rng` — the shipping behaviour is unchanged by the built patch.

`-M esp32s3,ybx-x4pro-wifi-stub=on`: `stub_semantics_all_ok: true`, all
11 pinned reads answer exactly the expected values:

```text
stats.magic        0x60035f00 = 0x59425230   crypto.key_readback 0x60034400 = 0x5a5a5a5a
stats.window_count 0x60035f04 = 0x00000006   mac.intr_get_empty  0x60033c3c = 0x00000000
stats.read_count   0x60035f08 = 0x00000005   mac.intr_still_empty 0x60033c3c = 0x00000000
stats.write_count  0x60035f10 = 0x00000003   tsf.frozen_low      0x6003500c = 0x00000000
stats.last_read    0x60035f18 = 0x6003500c   tsf.still_frozen    0x6003500c = 0x00000000
stats.last_write   0x60035f1c = 0x60035010
```

mtree under the stub: six `esp32s3.wifi_stub.*` owners
(`fe, modem_coex, tx_crypto, wdev_high, wdev_low, wlan_mac`) and WDEV shows
`[wdev_high, wdev_low, misc.esp32s3.rng]` — RNG still co-owns its word.
Direct qtest proof the TRNG survived the mapping (`-S`, CPU frozen):
`readl 0x6003507c` → `0x8c3da975`, then `0x79c83e31` — non-zero and
different, i.e. the real RNG answers, not the stub.

## Access tap — how to run it and its one quirk

The short form `-global misc.esp32s3.wifi_stub.x-radio-tap=on` **cannot
work**: `system/qdev-monitor.c:1188` splits the shorthand at the first dot
(`%63[^.=]`), so the driver becomes `misc` →
`warning: global misc.esp32s3.wifi_stub.x-radio-tap has invalid class name`.
Use the long form (measured, real log lines):

```sh
… -machine esp32s3,ybx-x4pro-wifi-stub=on \
  -global driver=misc.esp32s3.wifi_stub,property=x-radio-tap,value=on \
  -qtest stdio
```
```text
qemu-system-xtensa: warning: [radio-stub] read 0x60033c3c = 0x00000000 (size 4)
qemu-system-xtensa: warning: [radio-stub] write 0x60034400 = 0x11223344 (size 4)
qemu-system-xtensa: warning: [radio-stub] write 0x60033c40 = 0xffffffff (size 4)
```

It logs the first touch of each absolute address once (per-device
GHashTable), so a factory boot with the tap on yields the complete register
inventory the Stage-2 gate needs.

## Host tests (real output)

```text
$ python3 -m unittest test_wifi_radio_stage1 -v      # in tools/ybx-system-emu
Ran 14 tests in 0.086s
OK
$ cd …/wifi-radio-stage1 && python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q
Ran 517 tests in 29.460s
OK (skipped=11)
```

## What the guest can now do that it could not before

Measured register-level; the *guest-visible* consequence still requires a
boot (see Unproven):

- Writes to modem registers latch and read back, so read-modify-write
  consistency loops terminate instead of seeing the silent zero-fill
  (`ESP32S3_IO_WARNING 0` path, `esp32s3.c:118`): `force_txrx_off` and FE enable polls,
  `hal_set_tx_pti`/`hal_set_wifi_default_pti`, EDCA/`hal_mac_tx_config_*`,
  `hal_crypto_get_key_entry` verifying what `hal_crypto_set_key_entry`
  wrote (pinned qtest case), `cnx_bss_alloc` wdev status writes.
- The four event registers answer "no event" *with correct W1C semantics*
  (clear-then-get stays empty), so driver waits on MAC/power interrupt
  events, TXQ drain and TSF/timer/beacon exit through the driver's own
  timeout paths with a visible error instead of undefined zero behaviour.
- The access tap gives a complete runtime register inventory per boot.

What still does not work, unchanged by Stage 1: **no scan, no
authentication/association, no BSSID/IP/MAC address, no DHCP, no DNS, no
TLS, no RX/TX frames, no Wi-Fi interrupt (source 0 stays with open_eth),
no coexistence, no modem sleep, TSF frozen forever**. The stats ABI is
4-byte-aligned reads only.

## Evidence each later stage needs (from the 0060 roadmap, now testable)

- Stage 1 acceptance beyond this lane: a factory boot on a built QEMU
  showing (a) the driver's own timeout message in the guest log and (b) a
  tap log covering at least the pinned literal sites
  (`0x60033c3c/40, 0x60033ca8, 0x60034400, 0x60035000/c/7c(rng)/118`).
- Stage 2 (scan): the Stage-1 tap log diffed against the TRM to validate
  every forced-zero choice; then RX descriptor-ring walk + `ETS_MAC` IRQ
  on a resolved source; evidence = guest log `wifi scanned ap number:X` +
  frame-hash-verified scan screen.
- Stage 3 (auth/assoc/crypto): key-entry effect (RC4/TKIP/CCMP + replay
  counters) — hardware crypto is the guest's only path, no shortcut exists.
- Stages 4/5/6 unchanged from 0060 (DHCP in-guest, TLS loopback pair,
  TSF/DTIM before any battery claim).

## Orchestrator runbook

```sh
git -C <pinned-checkout> apply --check <this>/experiments/0071-s3-wifi-radio-stage1.patch
# or apply + rebuild in a fresh copy, then:
python3 tools/ybx-system-emu/probe_x4pro_wifi_radio_qtest.py \
  --qemu-root <rebuilt-root> --with-stub --out /tmp/ybx-wifi-radio/capability-0071.json
```

The live `ybx-x4pro-browser-8784` container keeps running the unpatched
binary; enabling the stub there means deploying a rebuilt QEMU **and**
adding `,ybx-x4pro-wifi-stub=on` to the machine line — not done here.

## Unproven / risks

- **No guest boot was performed in this lane** (forbidden): which driver
  waits actually break the boot, and whether the boot ends in a visible
  timeout, remain the orchestrator's first measurement.
- Forced-zero range correctness rests on function-name evidence
  (`*_get_event`/`*_clr_event` pairs), not on the S3 TRM; the tap log must
  verify before any default-on discussion. A bit that is really RW from the
  blob's point of view would now read 0 forever — the stub cannot fabricate
  it, only be conservatively wrong.
- Equal-priority subregions over `esp32s3.iomem` rely on the same overlap
  rule as every other YBX peripheral; mtree shows the split correct at
  `0x6003507b/7c/80`.
- The GHashTable tap dedups per address for the whole run (deliberate; a
  flood log would drown the boot log).
- 1/2-byte accesses inside the stats block fall through to plain storage
  reads (always 0) and are counted — stats are a 4-byte host ABI only.
- Register names for the `0x60030000-0x60035fff` cluster stay inferred
  (no public IDF base defines; same caveat as 0048/0060).
