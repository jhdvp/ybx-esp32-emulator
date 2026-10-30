# 0048 — ESP32-S3 Wi-Fi radio/PHY: is real emulation reachable in QEMU? (batch 2, wifi-radio lane)

Date: 2026-10-30. Host-only. No firmware was modified, no guest was booted by this
experiment, no container other than a disposable `--network none` one was created,
and the shared browser emulator (`:8784`, `:8785`) was not touched.

## Verdict (short)

1. Genuine Wi-Fi radio emulation is **not present and not partially present** in the
   patched QEMU (`espressif/qemu` @ `febae18` + YBX patches). The whole modem register
   space is served by a silent zero-fill catch-all.
2. The gap is now **measured, not assumed**: two host-only tools produce the register
   inventory the factory image really drives and the coverage QEMU actually answers.
3. A credible non-fake route exists, but it is a **multi-stage MAC-model project**, not
   a device-list switch. The first stage worth doing is a register *tap*, whose output is
   the dataset needed to write a real model — and which by itself still gives no network.
4. Nothing in this experiment shows scan, auth, DHCP, or a single 802.11 frame working.

## What was built

| File | Role |
| --- | --- |
| `tools/ybx-system-emu/wifi_radio_inventory.py` | Static evidence from the factory ELF: which radio layers are linked, which modem MMIO each function actually reads/writes (Xtensa `l32r` literal pools), and how QEMU's `info mtree` classifies each window. |
| `tools/ybx-system-emu/probe_x4pro_wifi_radio_qtest.py` | Live QEMU capability probe: `qtest readl` over radio registers plus `info mtree` with and without `-nic user,model=open_eth`, and `-device help` radio-type search. CPU never runs; every container is `--network none`. |
| `test_wifi_radio_inventory.py`, `test_probe_x4pro_wifi_radio_qtest.py` | 21 unit tests on synthetic fixtures. No ELF, no Docker, no QEMU needed. |

Reproduce:

```sh
mkdir -p /tmp/ybx-wifi-radio
T=$HOME/.platformio/packages/toolchain-xtensa-esp-elf/bin
# 1. live QEMU capability probe (writes capability.json, prints the verdict)
python3 tools/ybx-system-emu/probe_x4pro_wifi_radio_qtest.py \
  --qemu-root "$HOME/.local/state/ybx-emulator-upstream/qemu-rominit-20261029" \
  --out /tmp/ybx-wifi-radio/capability.json
# 2. the probe's mtree capture is what the static inventory classifies against
printf 'info mtree\nquit\n' | docker run --rm -i --network none --platform linux/arm64 \
  -v "$HOME/.local/state/ybx-emulator-upstream/qemu-rominit-20261029:/qemu:ro" \
  ybx-qemu-builder:20261029 sh -lc \
  'timeout -k 2s 8s /qemu/build-ybx/qemu-system-xtensa -M esp32s3 -L /qemu/build-ybx -S -nographic -serial null' \
  > /tmp/ybx-wifi-radio/mtree.txt
# 3. static inventory of the factory ELF
python3 tools/ybx-system-emu/wifi_radio_inventory.py \
  --elf "$HOME/.local/state/xteink-agent-workspaces/fw-092c2be5-symbols-20261029/.pio/build/x4pro/firmware.elf" \
  --nm $T/xtensa-esp32s3-elf-nm --objdump $T/xtensa-esp32s3-elf-objdump \
  --mtree /tmp/ybx-wifi-radio/mtree.txt --out /tmp/ybx-wifi-radio/inventory.json
python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py'   # 184 tests OK
```

Probe wall time 12.9 s for four QEMU invocations; every one uses `-S`, so the guest CPU never
executes an instruction and each container is killed by `timeout -k 2s 5s/8s` inside
`--network none`. Inventory 4.0 s. QEMU binary SHA-256 starts `4106cff2795eaaaa`.

## Evidence 1 — the shipped image really contains a full radio stack

`nm` of the X4 Pro ELF (25 846 defined symbols):
`esp_wifi_api` 123, `wifi_core_hal` (`hal_mac_*`, `hal_crypto_*`, `tsf_hal_*`, `wDev*`) 132,
`phy` 269, `coexistence` 292, `network_stack` (lwip/esp_netif/dns) 367, `tls` (mbedTLS) 312.
`wired_eth` = 1 (`esp_eth_ioctl` only) — no Ethernet MAC driver, so the wired path is not
an option for this image. User-visible Wi-Fi entry points:
`src/activities/network/WifiSelectionActivity.cpp:491` and `src/main.cpp:424` call
`WiFi.begin()`, and `src/network/WifiPowerSaveGuard.h` exists, i.e. modem sleep is used.

## Evidence 2 — modem MMIO the factory code drives (from its own literal pools)

`l32r` operands are the addresses the instruction genuinely dereferences, so this table is
machine-code evidence, not a header reading. Counts are distinct register words, not RAM.

| Window | Range | Accesses | Distinct addrs | Functions (examples) |
| --- | --- | --- | --- | --- |
| WIFI_MAC | 0x60033000-0x60033fff | 154 | 90 | `hal_mac_set_bssid`, `mac_txrx_init`, `mac_last_rxbuf_init`, `hal_agreement_add_rx_ba` |
| WDEV | 0x60035000-0x60035fff | 125 | 32 | `tsf_hal_set_timer_enable`, `hal_mac_tsf_reset`, `cnx_bss_alloc`, `esp_random` |
| FE (PA/antenna) | 0x60006000-0x60006fff | 125 | 62 | `ant_wifitx_cfg`, `ant_wifirx_cfg`, `bt_bb_v2_init_cmplx` |
| RTCCNTL | 0x60008000-0x600083ff | 118 | 43 | `esp_clk_init`, `esp_clk_slowclk_cal_get` |
| BT_BB (coex peer) | 0x60011000-0x60011fff | 62 | 41 | `bt_bb_corr_set`, `bt_bb_detect_set` |
| MODEM_COEX | 0x60031000-0x60031fff | 35 | 16 | `coex_pti_v2`, `btdm_sleep_clock_sync` |
| SYSCON | 0x60026000-0x600263ff | 25 | 11 | `btbb_wifi_bb_cfg2`, `phy_module_enable`, `periph_ll_wifi_module_enable_clk_clear_rst` |
| WIFI_TX_CRYPTO | 0x60034000-0x60034fff | 34 | 20 | `hal_crypto_set_key_entry`, `mac_tx_set_htsig` |
| NRX | 0x6001cc00-0x6001cfff | 10 | 7 | `register_chipv7_phy`, `phy_freq_correct` |
| BB | 0x6001d000-0x6001dfff | 3 | 3 | `spur_coef_cfg_new`, `phy_freq_correct` |

The 0x60030000-0x60035fff cluster has no public base define in IDF (`reg_base.h` jumps from
`DR_REG_UART2_BASE 0x6002E000` to `DR_REG_USB_SERIAL_JTAG_BASE 0x60038000`,
`.../soc/esp32s3/register/soc/reg_base.h:48-49`); QEMU itself confirms it is the radio block,
because it places the TRNG inside it with the comment
`ESP32S3_RNG_BASE 0x6003507C /* (DR_REG_WDEV_BASE + 0x07c) */`
(`include/hw/misc/esp32s3_rng.h:26`). IDF selects its Wi-Fi HAL for MAC v3 hardware
(`esp_wifi/include/esp_wifi_he_types.h:44`, `CONFIG_SOC_WIFI_MAC_VERSION_NUM == 3`).

## Evidence 3 — what QEMU answers today

`info mtree` for `-M esp32s3` (96 regions) classified per window by
`classify_window()` — `zero_fill_catchall` means the only owner is `esp32s3.iomem`:

| Window | Status | Owner |
| --- | --- | --- |
| FE2, FE, RTC_I2C, BT_BB, NRX, BB, MODEM_COEX, WIFI_MAC, WIFI_TX_CRYPTO | `zero_fill_catchall` | none |
| WDEV | `partially_modelled` | `misc.esp32s3.rng` only (4 bytes of 4 KB) |
| SYSCON | `ram_blob` | `esp32s3.apbctrl` is plain RAM (`esp32s3.c:508-515`) |
| RTCCNTL, PMS, INTMATRIX | `modelled` | `misc.esp32s3.rtc_cntl`, `.pms`, `.intmatrix` |
| EMAC @0x600CD000 | `partially_modelled` | `open_eth.regs`, `open_eth.desc` |

qtest reads (16 hex words, all as predicted → `reads_all_as_expected: true`):
`0x60033000`, `0x60033080`, `0x60033c34`, `0x60031050`, `0x60034400`, `0x6003505c`,
`0x6001d054`, `0x6001ccd4`, `0x6000607c`, `0x60011000`, `0x6002600c` → **all 0x0**;
controls `0x6003507c` → random word (RNG is live), `0x6002607c` → `0x96042000`,
`0x600cd000` → `0xa000`, `0x600c2000` → `0x6`.

Three consequences that matter for the guest:

1. **The failure is silent.** `esp32s3_io_read()` returns 0 for anything it does not model and
   the warning is compiled out (`ESP32S3_IO_WARNING 0`, `esp32s3.c:118`, `esp32s3.c:532-549`;
   the region covers `0x60000000 + 0xd1000`, `esp32s3.c:750-753`). The driver sees "all
   status bits clear, no interrupt pending" forever instead of a bus fault. That is exactly the
   unrecoverable state the browser had to escape from with QMP reset (`99243efb`).
2. **The Wi-Fi interrupt source is already taken.** `ETS_WIFI_MAC_INTR_SOURCE = 0`
   (`include/hw/xtensa/esp32s3_intc.h:26`) is reused as the EMAC line:
   `#define ETS_ETH_MAC_INTR_SOURCE ETS_WIFI_MAC_INTR_SOURCE` (`esp32s3_intc.h:150`), and the
   EMAC is instantiated unconditionally (`esp32s3.c:367-385`, `open_eth` present in mtree even
   with no `-nic`). Any radio model must resolve this collision first.
3. **Upstream's own C6 "modem" is only a clock stub** — `hw/misc/esp32c6_modem.c` is 112 lines
   that pin clock bits so IDF assertions pass (lines 22-46), wired at
   `hw/riscv/esp32c6.c:317,454-459`. It carries no MAC, no baseband, no netdev. So even the
   newest Espressif machine is precedent for *stub*, not for *radio*.
4. `qemu-system-xtensa -device help` (334 device types) contains **no radio type at all**
   (`wifi_device_types_in_qemu: []`); the only Ethernet-capable model wired into this machine is
   the OpenCores `open_eth`, which works with `-nic user,...` (slirp regions appear in mtree).

## 802.11 vs TCP/IP bridge — the actual decision point

Bridging "above the radio" is the cheap-looking option and it is blocked by the guest itself:

- The factory image programs **hardware WEP/TKIP/CCMP key entries** in the modem block
  (`hal_crypto_set_key_entry`, `hal_crypto_get_key_entry`, `hal_crypto_clr_key_entry` at
  `0x60033800-0x60033840` and `0x60034400-0x60034424`). Plaintext frames never reach the host:
  they are encrypted by the MAC hardware on TX and decrypted on RX. So "just convert 802.11
  headers to Ethernet and hand them to slirp" requires implementing the wdev crypto engine
  (RC4/TKIP and AES-CCMP with hardware replay counters) or the guest's own decrypt step fails.
- Frame exchange is descriptor-driven: `mac_txrx_init` / `mac_last_rxbuf_init` program RX
  buffer descriptor rings in guest RAM (`0x60033080-0x6003312c`), and the MAC owns/returns
  descriptors. Any RX injection must walk that ring, honour the ownership bit, and raise the
  level IRQ on source 0 — i.e. it is a real MAC model, not a netdev glue.
- Timing/TSF: `tsf_hal_*` and `hal_mac_tsf_reset` use `0x6003500c-0x6003511c`; beacons, DTIM
  and modem sleep (`WifiPowerSaveGuard.h`, `btdm_sleep_clock_sync`) depend on a running TSF and
  rcalendar. A frozen TSF makes power save and TBTT paths hang, so the model needs QEMU timers.
- Coexistence: 292 `coex*` symbols and `MODEM_COEX`/`BT_BB` traffic mean the BT/Wi-Fi arbiter is
  live in the image. It must be stubbed *consistently* (grant/deny never blocks Wi-Fi), not zeroed.

The alternative bridges are not credible for this guest:

| Route | Status |
| --- | --- |
| A. Model MAC+WDEV crypto, convert mgmt frames host-side, forward data to a QEMU netdev | Real radio emulation. Only route that keeps the factory binary untouched. Largest effort. |
| B. Ethernet under the radio (`open_eth` + slirp, already working) | Needs `esp_eth` + PHY driver in the image: `wired_eth = 1` symbol. Would be a *different firmware*, not the factory guest. |
| C. Replace the guest Wi-Fi stack with a host proxy | Faking. Excluded by the mission and by `AGENTS.md` ("don't fake successful network"). |
| D. Binary-only `esp-emu` (SoftAP/NAT) | Already the only engine with proven YBX app boot; closed, cannot be inspected or extended here (`QEMU_BRINGUP.md`). |

## Staged roadmap (each stage has a gate and an explicit "still not claimed")

**Stage 0 — done here.** Instrument the gap: register inventory + coverage verdict.
Gate: this file and the two JSON reports. Still not claimed: any radio behaviour.

**Stage 1 — modem register tap (next, small, highest value per line).**
Replace the silent zero-fill for `0x60030000-0x60035fff`, `0x60005000-0x60006fff`,
`0x6001CC00-0x6001DFFF` with a QEMU device that (a) records `(timestamp, addr, rw, value, size,
bits)` per access, (b) answers documented reset values instead of 0, and (c) can be enabled per
window. Precedent to follow rather than copy: the YBX EPD/BME/GT911 patches already in
`tools/ybx-system-emu/patches/` (e.g. `0042-s3-epd-busy-gpio-pcpu-irq.patch`).
Gate: with factory firmware booted, the tap log shows `hal_mac_init`/`phy_module_enable`
sequences and the guest reaches an *observable* driver timeout instead of a silent hang.
Still not claimed: scan, association, packets.
Cost: one QEMU source file plus a patch entry; no firmware change; the tap is opt-in so default
behaviour is unchanged.

**Stage 2 — scan.** Model enough to deliver beacons: channel set from `hal_*` (the driver's
channel/agility registers), RX descriptor ring walk, `ETS_MAC`/`ETS_BB` level IRQ on a resolved
source, plausible `SYSCON_WIFI_BB_CFG` values (today `ram_blob` → 0, and IDF reads PHY init
parameters there), plus RX-OK status words so `esp_wifi_scan_get_ap_records()` is non-empty.
Host side: a "virtual AP" that only emits 802.11 management frames (beacon/probe response) with
the SSIDs the QA harness configures.
Gate: real guest log line `wifi scanned ap number:X` and the factory scan screen renders those
SSIDs; PNG hash change like the proven `0042` touch path.
Still not claimed: authentication, IP, TLS.

**Stage 3 — auth/assoc.** Implement TX descriptor consumption for auth/assoc/action frames,
assoc-response and EAPOL delivery, and the wdev key-entry semantics (store key index/CCMP TX
MIC material; encrypt on TX, decrypt+verify on RX) so the guest's own WPA2 supplicant completes.
This is where "802.11 vs bridge" is paid for: no shortcut without a fake.
Gate: guest reaches `SYSTEM_EVENT_STA_GOT_IP`-adjacent state with a 4-way handshake observed by
the tap and *no* host-side decryption.

**Stage 4 — DHCP/IPv4 + DNS.** Once data frames flow, attach the radio model to an existing
QEMU backend: `slirp` gives in-built DHCP (10.0.2.0/24) and DNS with zero host exposure
(the existing `-nic user,model=open_eth` proves the backend is compiled in); `stream`/`socket`
netdev or `tap` for a real LAN. Keep CI containers `--network none` and grant network only as an
explicit human opt-in.
Gate: `HttpDownloader`/`CrossPointWebServer` paths exercised against a loopback service.

**Stage 5 — TLS and app-level flows.** No radio work: mbedTLS (312 symbols) is guest-side;
verify `TlsTransport` success and failure against a local test server.
Gate: one success and one certificate-failure case.

**Stage 6 — timing/power/coex.** Running TSF + timer IRQs, DTIM/modem-sleep windows, coex
grant stub. Needed before any claim about battery or sleep behaviour; not needed for a first
scan/connect demo.

## Risks and limits

- Stage 1+ requires **patching and rebuilding QEMU**, which this lane is forbidden to do
  (`qemu-rominit-20261029` is read-only). Everything here is host-side measurement only; the
  register tap must land in the developer QEMU checkout, and per `AGENTS.md` a real guest boot
  plus human hardware verification remain outstanding.
- Closed-source driver: `net80211`/`wdev`/`phy` blobs are precompiled, so bit meanings must come
  from the S3 TRM and from tap logs, not from IDF source. Register semantics guessed today would
  become a fake.
- Address names: `MODEM_COEX`, `WIFI_TX_CRYPTO` and the 0x60030000 page boundaries are
  **inferred** from observed accesses plus `esp32s3_rng.h:26`; only FE/FE2/NRX/BB/SYSCON/RTCCNTL
  are IDF-defined bases. Treat the names as labels, not documentation.
- The EMAC/interrupt reuse (`esp32s3_intc.h:150`) and `apbctrl` RAM blob (`esp32s3.c:508-515`)
  are pre-existing upstream decisions that a radio model must not silently break; they also mean
  today's S3 machine is not a faithful silicon map.
- No packet, association, scan result or IP address was produced or observed by this experiment.
