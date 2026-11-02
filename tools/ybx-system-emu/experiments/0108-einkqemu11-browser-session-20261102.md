# 0108 — X4 Pro migration to eink-qemu 0.9.2 / QEMU 11.1.2

Date: 2026-11-02. This is an **isolated, optional second engine**, not a production cutover. Stable YBX bridge **192.168.10.239:8784 stays on the audited 32-patch QEMU 9-based backend** with original persistent guest Flash/SD, old SHA `7b729fba1ceefdc67a55dc9ddd6d359699e16174a52d741ea243849c35e2accc`.

## Upstream identity, provenance and legal notes

- Upstream: https://github.com/serialx/eink-qemu at `d1b24a5` (2026-10-25), upstream QEMU **11.1.2**, project release **0.9.2**. Xteink X4 Pro is an explicit native `-machine x4pro`, not the generic ESP32-S3 machine used by our older core.
- Official arm64 Linux asset: `eink-qemu-xtensa-softmmu-0.9.2-aarch64-linux-gnu.tar.xz`. Audited SHA against the release's published `eink-qemu-0.9.2-checksum.sha256`. Embedded `qemu-system-xtensa` SHA-256 **`8fce2150c091ceb7b644ac7603169620e9fc171ebaa37bed8aece2a457dcbd6a`**.
- Installed locally ONLY in the private artifacts folder: `~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/reference-eink-qemu-20261102/linux/qemu/`. No external binaries, firmware, ROM or SD images are committed into YBX Git.
- QEMU distribution is GPLv2 and contains components with their own compatible licenses. Maintain original copyright/attribution if redistributing or modifying its native model. This migration only calls the separate, official native executable; it does not copy the upstream Wi-Fi code into our repo.
- Reproducible runtime recipe `Dockerfile.einkqemu11-runtime` builds image `ybx-eink-qemu-runtime:0.9.2` on existing YBX builder, installing libpng16, SDL2 and libX11. Official binary is mounted read-only; launcher rejects an unexpected binary SHA. Network is permitted ONLY as opt-in virtual guest user-mode NAT. Build image needs package access unless cached.

## Functional parity verified by unmodified X4 Pro firmware

| Hardware / workflow | Actual result on QEMU 11.1.2 |
|---|---|
| Original Factory firmware | Native ESP-ROM, PSRAM, Home UC8279 at 480×800, real USB-CDC UART; PNG from QEMU HMP; no binary patches |
| Original CrossPoint 2.0.0rc | ESP32-S3 boot / 8 MiB PSRAM, identifies UltraChip UC8279 rev0x68; native Home panel capture |
| GT911 and browser touch | QMP `input-send-event` at native absolute pointer coordinates, real Factory GUI repaint; no mock HTML-driven frames |
| Wi-Fi MAC / scan | Using `-nic user,model=esp32_wifi`, real Factory navigation Home → More → File Transfer → Join a Network → `Wi-Fi Networks`; shows **PICSimLabWifi / Espressif / MasseyWifi** and station MAC `52:54:00:12:34:56` |
| Association and service | Original Factory selects open PICSimLabWifi, UART BSSID connect, `connected=1`, HTTP/WebSocket port80 server launched |
| Real TCP via NAT | QEMU HMP `hostfwd_add tcp:127.0.0.1:18082-:80` (bound **only inside disposable Docker container**), Python HTTP GET returns **HTTP 200** with **2,090 response bytes**; `hostfwd_remove` confirmed, proof `/tmp/ybx-eink11-nat-http-final.json` |
| YBX web browser | New `serve_einkqemu11_browser.py` maps native x4pro QMP to existing YBX `/`, `/screen.png`, `/api/state`, `/api/tap`, `/api/uart`, `/api/firmware`, `/api/reboot`. UC8279 native PNG parser implemented without Pillow in container; RGB hashes are **not** interchangeable with earlier raw SSD1677 hashes. `/api/telemetry` correctly returns unavailable for old model-specific EPD counters |
| Private preview | `127.0.0.1:8806` and (Wi-Fi NAT) `127.0.0.1:8807`, bound host loopback only. The browser accepts real taps and updates its panel. These are **not production** URLs |
| Saved guest Flash/SD | Host persistent volume `flash.bin` + `sd.img` is resumed without overwriting; partial pair/symlink/wrong sizes rejected. `selected-firmware.json` stores displayed firmware selection; a Docker stop/recreate kept disk checksums and displayed name (agent concurrently switched firmware during a test; do not infer exact selected firmware from stale snapshots) |
| Virtual Power | Optional native `/machine/x4pro-keys` Power QMP control, server-owned 1.2-second watchdog and stale-timeout protection. Not enabled on 8806/8807 or stable 8784 |

## Release blocker: inkMOD 1.1.10 regression

Original **inkMOD 1.1.10** on the new `-machine x4pro` fails **before pressing virtual Power**, so no production migration is authorized yet.

- Isolated `127.0.0.1:8876`, exact original flash SHA `3fb63a3101fd662a3914c836a2c5ef4a571bb89a54eb1af4b1834326007258f0`, with guest-specific QMP Power enabled.
- ROM boots, but CPU1 reports `Guru Meditation Error (LoadProhibited)` with PC **`0x4204541c`**, `EXCVADDR=0x000060f0`, ELF marker `ccf8eff5f`. After restart repeats, then invalid debug exception. UART `rst:0xc (RTC_SW_CPU_RST)`.
- A later native virtual Power press is acknowledged and automatically released, but **no `rst:0x5 (DSLEEP)`**, and no working inkMOD screen. The crash predates pressing Power; it is NOT evidence that the Power implementation caused the crash.
- Previous YBX QEMU 9.x accepted inkMOD 3/3 controlled EXT1 wake cycles and native home frames, so this is a **true compatibility regression** and a blocker to replacing 8784.
- Must isolate actual PC/function with matching inkMOD ELF/symbols if available, trace memory/I2C/RTC/PSRAM model and implement hardware-correct fixes in a distinct upstream QEMU11 integration branch; do not edit flash or fake a successful boot.

## Operational safeguards / next stage

- The current QEMU11 engine does **not** expose the old ROM-swapping endpoint: `/api/roms` lists no alternatives; `/api/rom` explicitly refuses. `/api/telemetry` does not invent new EPD counters.
- `start_einkqemu11_browser.sh` refuses port 8784, verifies exact QEMU binary SHA, publishes host **127.0.0.1 only**, mounts original ROM/firmware/QA read-only and persistent guest disks to a private RW directory. `YBX_EINK11_WIFI=1` enables NAT; no host bridge by default. `YBX_EINK11_POWER=1` enables the bounded virtual Power key only on the private port.
- Different screenshots can have a **different** SHA because this is the native UC8279 RGB panel, rather than raw SSD1677 frame space; use content-aware parity tests and native guest events, not force-matching old checksum.
- The browser is a small compatibility layer for experimental testing, not a complete rewrite of all old admin features; all existing stable functionality and user/agent uncommitted `0095` report are preserved.
- Next: resolve inkMOD crash, verify original CrossPoint 1.6.5 in QEMU11, run full GT911 navigation/SD integrity and repeated sleep/wake, verify security and lifecycle before scheduling explicit rollback-backed cutover. **Do not switch 8784 in this PR**.
