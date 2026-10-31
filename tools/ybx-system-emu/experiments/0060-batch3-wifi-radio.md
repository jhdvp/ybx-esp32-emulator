# 0060 — Wi-Fi radio: pin the factory register accesses, design the smallest non-fake stub (batch 3, wifi-radio lane)

Date: 2026-10-31. Host-only: no guest was booted, no container was created, the
live emulator (`ybx-x4pro-browser-8784`) was not touched, the pinned QEMU
checkout was not modified or rebuilt. The QEMU change in this file is a
**proposal validated by `git apply --check` only** — it has not been compiled
or run, and that is stated again in "Unproven" below.

## What changed

| File | Role |
| --- | --- |
| `tools/ybx-system-emu/wifi_radio_inventory.py:204` | `FACTORY_BASELINE`: per-window l32r access counts, distinct addresses, observed ranges, example functions and *why the driver touches each window*, pinned from the factory X4 Pro ELF itself. |
| `tools/ybx-system-emu/wifi_radio_inventory.py:329` | `verify_baseline()`: compares any new inventory run against the pins (window fields, totals, key-register counts). |
| `tools/ybx-system-emu/wifi_radio_inventory.py:406` | `key_register_counts()`: per-address access counts + functions for the eight pinned registers; folded into `build_report()` (line 467) as `key_register_counts` + `baseline_mismatches`. |
| `tools/ybx-system-emu/probe_x4pro_wifi_radio_qtest.py:52` | `STUB_OPS` + `stub_protocol()` (line 76), `classify_stub()` (95), `verdict_stub()` (109), `machine_supports_property()` (197), and the `--with-stub` main path (244): qtest script that exercises the proposed stub's semantics. Needs the rebuilt QEMU — for the orchestrator to run. |
| `tools/ybx-system-emu/experiments/0060-s3-wifi-radio-model.patch` | The proposed device model (423 lines, 4 files). Validated read-only, not applied. |
| `tools/ybx-system-emu/test_wifi_radio_inventory.py:142,216` | Pins: window counts (154/90 etc.), total 692, symbol counts, key-register counts, plus `FACTORY_EXCERPTS` (line 126) — real objdump lines proving the pinned literal accesses. |
| `tools/ybx-system-emu/test_probe_x4pro_wifi_radio_qtest.py:97` | `StubProtocol`: protocol shape, write-ack filtering, expected semantics (key read-back, empty events, frozen TSF, counters), mtree ownership including the RNG carve-out. |

## Evidence 1 — the pins reproduce from the factory image

ELF: `/Users/yaraslauhancharou/.local/state/xteink-agent-workspaces/fw-092c2be5-symbols-20261029/.pio/build/x4pro/firmware.elf`
`shasum -a 256` → `7e040053768db2d6559305dd26433b0ee8264de143202d3c72ace582e7ba98e5`.
Tools: PlatformIO `toolchain-xtensa-esp-elf/bin/xtensa-esp32s3-elf-{nm,objdump}`.
Command (real output below):

```sh
python3 tools/ybx-system-emu/wifi_radio_inventory.py \
  --elf ~/.local/state/xteink-agent-workspaces/fw-092c2be5-symbols-20261029/.pio/build/x4pro/firmware.elf \
  --nm ~/.platformio/packages/toolchain-xtensa-esp-elf/bin/xtensa-esp32s3-elf-nm \
  --objdump ~/.platformio/packages/toolchain-xtensa-esp-elf/bin/xtensa-esp32s3-elf-objdump \
  --out /tmp/ybx-wifi-b3/inventory2.json
```

Runtime 2.95 s, exit 0. Measured per-window counts (window, l32r literal
sites, distinct words, observed range — pinned in
`FACTORY_BASELINE["windows"]` and asserted in `PinnedFactoryBaseline`):

| Window | Accesses | Distinct | Observed range | Why the driver touches it (function-name evidence) |
| --- | --- | --- | --- | --- |
| WIFI_MAC 0x60033000 | 154 | 90 | 0x60033000-0x60033d14 | MAC init/deinit, BSSID, RX rings, TXQ state, interrupt get/clear, EDCA/PTI (`hal_mac_init`, `hal_mac_set_bssid`, `mac_txrx_init`) |
| WDEV 0x60035000 | 125 | 32 | 0x60035000-0x6003511c | TSF get/set, 4 HW timers, TBTT/light sleep, RX-beacon PTI, power-int get/clear; 0x60035000 is the hottest word (43 sites: `cnx_bss_alloc`, `lmacSetTxFrame`, `ppTxPkt`…) |
| FE 0x60006000 | 125 | 62 | 0x60006000-0x600061e4 | PA/antenna switch + TX digital gain per TX/RX turn (`ant_wifitx_cfg`, `ram_set_pbus_reg`, `force_txrx_off`) |
| RTCCNTL | 118 | 43 | 0x60008000-0x600081fc | clock tree/slow-clamp calibration feeding modem-sleep timing (`esp_clk_init`, `esp_clk_slowclk_cal_get`) |
| BT_BB | 62 | 41 | 0x60011008-0x600118b0 | coex peer baseband parameters (`bt_bb_corr_set`, `bt_bb_detect_set`) |
| MODEM_COEX | 35 | 16 | 0x60031000-0x60031530 | PTI arbitration table + scheduler + sleep-clock sync (`coex_pti_v2`, `btdm_sleep_clock_sync`) |
| WIFI_TX_CRYPTO | 34 | 20 | 0x600342f8-0x60034424 | hardware crypto key table 0x60034400-0x60034424 + TX PLCP/HT-SIG vectors (`hal_crypto_{set,get,clr}_key_entry`) |
| SYSCON | 25 | 11 | 0x60026000-0x600260c0 | wifi module clock enable/reset (`phy_module_enable`) |
| NRX | 10 | 7 | 0x6001cc0c-0x6001cd0c | RX calibration (`register_chipv7_phy`, `phy_freq_correct`) |
| BB | 3 | 3 | 0x6001d018-0x6001d054 | spur coefficients / freq correct (`spur_coef_cfg_new`) |
| FE2 | 1 | 1 | 0x600050f0 | one power-up touch (`rtc_sleep_pu`) |

Total 692 literal sites; symbols unchanged vs 0048 (`total_defined` 25846,
`esp_wifi_api` 123, `wifi_core_hal` 132, `phy` 269, `coexistence` 292,
`network_stack` 367, `tls` 312, `wired_eth` 1). These numbers reproduce the
0048 batch-2 table exactly, so both batches measure the same thing.

Key-register pins (real `key_register_counts` output, verified by
`verify_baseline` → `baseline_mismatches: []`):

```text
0x60034400 x3  hal_crypto_clr_key_entry, hal_crypto_get_key_entry, hal_crypto_set_key_entry
0x60033c3c x1  hal_mac_interrupt_get_event
0x60033c40 x3  hal_deinit, hal_init, hal_mac_interrupt_clr_event
0x60033ca8 x2  hal_mac_get_txq_state
0x60035000 x43 (36 distinct cnx_/lmac_/ppTx/hostap_/pm_ functions)
0x6003500c x7  hal_get_sta_tsf, hal_get_tsf_time, hal_mac_tsf_reset, hal_set_sta_tbtt, hal_set_sta_tsf, tsf_hal_get_counter_value
0x6003507c x3  esp_random (+ WebSocketsServerCore::begin, hal_random)
0x60035118 x1  hal_pwr_interrupt_get_event
```

`test_wifi_radio_inventory.py:126` embeds the verbatim objdump lines (tabs
collapsed to spaces) for `hal_mac_interrupt_get_event`,
`hal_mac_interrupt_clr_event`, `hal_get_tsf_time` and the `hal_deinit` clear
site, so the attribution rule itself (current function header, *not* the
literal-pool decoration — objdump annotates pools with the neighbouring
symbol, e.g. `<mac_last_rxbuf_init+0xf8>` inside `hal_deinit`) is pinned.

## Evidence 2 — the proposed device model (0060 patch)

`experiments/0060-s3-wifi-radio-model.patch`, machine property
`-M esp32s3,ybx-x4pro-wifi-stub=on` (default off ⇒ behaviour byte-identical to
today). New device `misc.esp32s3.wifi_stub` maps six windows over
`esp32s3.iomem` at equal priority, same as every other peripheral:

`modem_coex 0x60031000/0x1000`, `wlan_mac 0x60033000/0x1000`,
`tx_crypto 0x60034000/0x1000`, `wdev_low 0x60035000/0x7C`,
`wdev_high 0x60035080/0xF80`, `fe 0x60006000/0x1000`.
The WDEV window is **split around `ESP32S3_RNG_BASE 0x6003507C`**
(`include/hw/misc/esp32s3_rng.h:26`) so `esp_random` keeps the real TRNG.
Inside `wdev_high`, `0x60035f00-0x60035f1c` is a read-only host stats block
(magic `YBR0`, window count, guest-visible read/write counters, last access
addresses). The factory image never touches 0x60035f00: the highest measured
WDEV literal is 0x6003511c.

Semantics:

1. Ordinary words are **register storage**: writes latch, reads return them
   (1/2/4-byte lane handling). This is what a real register file does and what
   `esp32s3.iomem` (`hw/xtensa/esp32s3.c:532-552`) refuses to do today.
2. Four ranges are **forced to read zero** — only where silicon, not the CPU,
   owns the value, each justified by a pinned literal site above:
   `0x60033c3c-0x60033c44` (MAC interrupt event get/clear),
   `0x60033ca4-0x60033cb0` (TXQ state get/clear),
   `0x6003500c-0x60035020` (TSF low/high incl. set words — a *frozen* TSF,
   never ticking), `0x60035118-0x60035120` (power-interrupt get/clear).
   Writes to them are counted but not stored; they can never latch an event.
3. **No interrupt is wired** (the `ETS_WIFI_MAC_INTR_SOURCE = 0` line belongs
   to `open_eth`, `esp32s3_intc.h:150` — the model deliberately does not
   touch that collision), no netdev is attached, and nothing in the device can
   report association, scan results or key take-effect.

Driver waits the design unblocks (static reasoning, to be confirmed by tap
logs once built):
read-back/consistency loops — `force_txrx_off` and FE enable polls,
`hal_set_tx_pti`/`hal_set_wifi_default_pti`, EDCA/`hal_mac_tx_config_*`,
`hal_crypto_get_key_entry` verifying what `set_key_entry` wrote, wdev queue
status writes from `cnx_bss_alloc`.
Waits it must NOT unblock (and does not, by construction): every hardware
*completion* event — RX descriptor ownership, beacon/TBTT, timer match
(`tsf_hal_set_timer_*`), MAC/power interrupt events, TXQ drain done,
4-way handshake timers. Those reads return zero forever, so the driver exits
them through its own timeout paths with a visible error instead of the silent
zero-fill hang (`ESP32S3_IO_WARNING 0`, `esp32s3.c:118`).

Validation performed (read-only, exactly as required):

```sh
git -C ~/.local/state/ybx-emulator-upstream/qemu-0043-agent-audit-20261030 \
    apply --check \
    ~/.local/state/xteink-agent-workspaces/ybx-emu-batch3-20261031/wifi-radio/\
tools/ybx-system-emu/experiments/0060-s3-wifi-radio-model.patch
```

→ exit 0, no output (`APPLY-CHECK-OK`), against the pinned checkout in its
current state (febae18 + 0042 + 0043 + 0037 + 0051 applied in the worktree).
The patch was **not applied** and the build directory was not rebuilt.

Host-side logic tests for the probe (`StubProtocol`) pin the exact qtest
sequence and expected answers (key write 0x5a5a5a5a reads back; MAC event get
stays 0 even after a W1C write; TSF stays 0 after a set attempt; counters
read 5 guest-visible reads / 3 writes; `last_read=0x6003500c`,
`last_write=0x60035010`).

## Orchestrator runbook (needs Docker + rebuild — outside this lane)

```sh
python3 tools/ybx-system-emu/probe_x4pro_wifi_radio_qtest.py \
  --qemu-root <checkout with 0060 applied and rebuilt> --with-stub \
  --out /tmp/ybx-wifi-radio/capability-0060.json
```

Acceptance for Stage 1: default machine keeps `radio_register_windows_unmodelled:
true`; `--with-stub` reports `stub_semantics_all_ok: true`, six
`esp32s3.wifi_stub.*` owners in mtree, `misc.esp32s3.rng` still co-owning
WDEV, and the guest boot reaches a *visible* driver timeout instead of the
silent hang. Anything beyond that is a new stage, not a claim of this patch.

## Staged roadmap (updated against 0048; each stage names its risk and the evidence needed)

- **Stage 1 — register stub + tap (this patch).** Risk: forced-zero set could
  be wrong (a bit that silicon self-clears may instead be RW in the blob's
  view) → guest behaves differently than on hardware; mitigated by default-off
  and the tap property. Evidence required: built QEMU, qtest semantics green,
  factory boot log showing the driver's own timeout, tap log of ≥ the pinned
  literal sites. Not claimed after Stage 1: anything above register level.
- **Stage 2 — scan.** Needs RX descriptor-ring walk + `ETS_MAC` IRQ on a
  resolved source + host-side management-frame emitter. Risk: the first place
  a "plausible value" could silently become a fake; every bit must come from
  the Stage-1 tap vs TRM. Evidence: guest log `wifi scanned ap number:X` and a
  frame-hash-verified scan screen like the proven EPD path.
- **Stage 3 — auth/assoc + wdev crypto.** Must implement key-entry effect
  (RC4/TKIP/CCMP incl. replay counters) because the guest encrypts/decrypts via
  hardware — no shortcut exists that is not a fake. Risk: largest closed-blob
  reverse-engineering surface; TSF/timer IRQs are prerequisites. Evidence: a
  WPA2 4-way handshake observed by the tap completing without host-side
  decryption shortcuts, `SYSTEM_EVENT_STA_GOT_IP`-adjacent state.
- **Stage 4 — DHCP/IPv4+DNS.** Attach Stage-3 model to slirp/tap. Risk:
  network egress in CI; keep containers `--network none` except explicit human
  opt-in. Evidence: DHCPACK *inside the guest* and DNS resolution, not host
  synthesis.
- **Stage 5 — TLS/app flows.** Guest-side mbedTLS work only; risk low;
  evidence: one success + one certificate-failure case against a loopback
  server.
- **Stage 6 — TSF, DTIM/modem-sleep, coex arbitration.** Needed before any
  battery/sleep claim; this patch deliberately freezes rather than fakes them.

## What remains unimplemented after this change (plainly)

The radio is still not emulated. No 802.11 frame is generated or consumed, no
scan, authentication, association or key take-effect; no WDEV crypto (TX
encryption / RX decrypt+replay), no TSF/timer/beacon/TBTT ticks, no Wi-Fi
interrupt (source 0 stays with `open_eth`), no coexistence arbitration, no
modem sleep, no DHCP/DNS/IP, no TLS. The stub only replaces silent zero-fill
with coherent register storage, always-empty event registers, and counted
accesses.

## Unproven / risks

- The stub has **not been compiled or executed**: no `meson.build` compile
  test, no qtest run happened in this lane (Docker and rebuilds are the
  orchestrator's). Behavioural claims above are design claims validated by
  construction (pure parser tests) and by `git apply --check`.
- Which driver waits actually block the factory boot cannot be measured in
  this lane (guest boot is forbidden here); the Stage-1 gate is the first
  measurement that will confirm or refute the unblock list.
- Register names/ranges for the 0x60030000-0x60035fff cluster stay inferred
  (no public IDF base defines; same caveat as 0048); only FE/FE2/NRX/BB/
  SYSCON/RTCCNTL are IDF-defined.
- Forced-zero range correctness rests on function-name evidence
  (`*_get_event`/`*_clr_event` pairs), not on the S3 TRM; tap logs must verify
  before this is ever enabled by default.
- 1/2-byte accesses to the stats block are rejected by the size/alignment
  guard and fall through to plain storage reads (always 0) — deliberate, but
  it means the stats ABI is 4-byte-only.

Verification command output for this lane (paste):

```text
$ python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q
Ran 304 tests in 14.118s

OK (skipped=7)
```
