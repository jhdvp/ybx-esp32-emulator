# 0107 — Migration candidate: eink-qemu 0.9.2 / QEMU 11.1.2

Date: 2026-11-02. **New upstream is the intended target for YBX; the live 8784 is NOT yet migrated** because original inkMOD 1.1.10 demonstrably panics under it. The work below is an actual verified prototype, not only an architectural proposal.

## Why replace the old 9.2 lineage

Our existing stable `192.168.10.239:8784` is a fork of Espressif QEMU 9.2.x with 32 individually audited local patches (SHA `7b729fba1ceefdc67a55dc9ddd6d359699e16174a52d741ea243849c35e2accc`). We had been implementing radio tracing, RTC, GT911, SSD1677 and device-timing functionality ourselves. Upstream `serialx/eink-qemu` already ports the ESP32-S3/C3 SoC and complete Xteink X4 Pro board model to **QEMU 11.1.2**, including UC8279 0x68 (and older UC8179) calibrated panel, GT911, SDMMC, RTC ext0/ext1 deep-sleep wake, MAC DMA and 802.11 virtual open AP, plus a native host-network NAT backend. This is objectively a better strategic base. No blanket merging of our old 32 patches: they target different QEMU memory/device and UI APIs and many features already exist upstream.

## Exact upstream pins, licenses, source

- Upstream: https://github.com/serialx/eink-qemu
- Main at inspection (2026-11-02): `d1b24a5fbc1bbae7ea31075460f63cd4c989c1dc` (2026-10-25 preparation of 0.9.2).
- Latest release available at inspection: **0.9.2, 2026-10-25**, based on **QEMU 11.1.2**.
- Official Linux aarch64 `xtensa-softmmu` release TAR SHA-256 **`6adc72273fd6fea75407ee312af8f21b9c83e67d88720515d56068e71e7f6300`**, independently compared with the upstream `eink-qemu-0.9.2-checksum.sha256`.
- Extracted actual `linux/qemu/bin/qemu-system-xtensa` SHA-256 **`8fce2150c091ceb7b644ac7603169620e9fc171ebaa37bed8aece2a457dcbd6a`**.
- Official macOS aarch64 archive SHA `267b73020a79d5757de877ddc0eab89b5c95a7ffe04a58e9eae283bc71d6f41d`; the binary *did not start on this Mac* because Brew GUI dependencies such as `/opt/homebrew/opt/libpng/lib/libpng16.16.dylib` were absent. **Do not install arbitrary brew packages or modify the Mac just to test the binary.**
- Created an isolated Linux/arm64 Docker test image `ybx-eink-qemu-runner:20261102`, extending the existing offline QEMU builder image with required `libpng16-16`, `libsdl2-2.0-0`, and `libx11-6` packages. Docker image built once. Original upstream binaries, firmware flash images and local SD images are untracked, kept outside the Git repo in `~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/reference-eink-qemu-20261102/`.
- Licensing: main QEMU-derived code GPL-2.0-or-later, with MIT-origin MAC/AP components and other individual notices. This PR only pins and runs the official separate release; no vendor binaries or firmware copied into our Git tree.

## Original CrossPoint 2.0.0rc: actual Wi-Fi association and HTTP, not a simulated web page

Started the **unmodified 16MiB** `crosspoint-2.0.0rc-x4pro-flash-16m.bin` on the official native Linux ARM64 QEMU executable, `-machine x4pro`, `-icount shift=0,sleep=off`, pristine private SD copy, `-nic user,model=esp32_wifi` inside an isolated Docker container. No hardware, production SD, firmware, or 8784 changes.

- UART shows `Device: xteink_x4_pro`, `SDMMC card mounted`, native UC8279 `VER=00 0F 68 01 00`, real panel refresh delays, the `[MEM]` checkpoint continuing for many virtual seconds, **no guest abort/panic**.
- Real QEMU QMP `query-mice` reports `GT911 touchscreen`, absolute input. QMP `screendump` returns a real **480×800 P6** framebuffer; the original Home has SHA-256 `baf5d2d37fd5082f40879ce62499ba6355d97416fb9056f101ccb79ac52d8448` of raw RGB pixels.
- QMP `input-send-event` with properly scaled absolute X/Y and mouse button actions navigated Home → **File Transfer** → **Join a Network**. The original firmware transitioned from `Scanning…` / 0 networks to **3 real virtual AP beacons**, listed as `PICSimLabWifi`, `Espressif`, `MasseyWifi`. MAC `52:54:00:12:34:56`.
- Tapped `PICSimLabWifi` and received the actual guest **connected File Transfer** screen, with `http://10.0.2.15/` and QR code. The 10.0.2.15 IP is assigned by QEMU user-mode NAT DHCP, not a manually inserted value or image overlay.
- Dynamically added **QEMU user-mode NAT hostfwd** via QMP HMP `hostfwd_add tcp::18080-:80` and from **inside the private Docker container** retrieved `http://127.0.0.1:18080/`: **HTTP 200**, `Content-Type: text/html`, `Content-Encoding: gzip`, `Content-Length: 2103`. This is a genuine original CrossPoint HTTP response. It is NOT published on the actual Mac LAN or production bridge.

## Working lightweight browser/QMP adapter (new implementation)

`eink_qemu11_preview_bridge.py` provides a standalone **localhost-only** HTTP surface using the upstream QEMU 11.1.2 native QMP, not the previous proprietary `ybx-*` QOM telemetry/SSD1677 hook. `/screen.png` calls real `screendump` and encodes RGB PPM to PNG losslessly with Python stdlib; `/api/tap` sends true absolute GT911 QMP events with down/up and bounded duration; `/api/state` reports QMP running status, observed UART Home/memory checkpoint and last frame SHA without inventing Wi-Fi connection state; `/api/uart` displays bounded guest log. The interactive browser frontend refreshes the true 480×800 native screen.

`start_eink_qemu11_preview.sh` verifies the exact published Linux TAR SHA, extracted binary SHA, upstream source commit (if checked out), and original flash/SD geometry before starting. Refuses reuse of an existing work directory, ports 8784, or reusing the named container. Launches Docker only on an isolated host loopback QMP port, and the preview web app binds 127.0.0.1 only. `--check` verifies pins and inputs without creating/stopping any Docker container. Its sample default local preview is **http://127.0.0.1:8810** and QMP **127.0.0.1:8811**. The new independent native X4 Pro container is `ybx-eink-qemu11-8810` and the live image passed browser HTTP 200, 480×800 PNG screenshot and true QMP touch operations. GT911 taps via the NEW HTTP bridge independently reached the original CrossPoint wireless workflow and the guest HTTP server answered HTTP 200 through its slirp portforward. The prior stable LAN service was **not touched**.

## Recreate the preview on this Mac

The official release artifact and Docker dependencies are intentionally **not** inside Git; install only the verified, checksum-locked upstream package. For an existing local `gh` login, with all tools installed:

```sh
BASE="$HOME/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/reference-eink-qemu-20261102"
mkdir -p "$BASE/linux"
cd "$BASE"
gh release download 0.9.2 -R serialx/eink-qemu \
  -p 'eink-qemu-xtensa-softmmu-0.9.2-aarch64-linux-gnu.tar.xz' \
  -p 'eink-qemu-0.9.2-checksum.sha256'
tar -xf eink-qemu-xtensa-softmmu-0.9.2-aarch64-linux-gnu.tar.xz -C linux
cd "$HOME/Projects/YBX/ybx-esp32-emulator"
docker build --platform linux/arm64 \
  -f tools/ybx-system-emu/third_party/Dockerfile.eink-qemu11-runner \
  -t ybx-eink-qemu-runner:20261102 .
bash tools/ybx-system-emu/start_eink_qemu11_preview.sh --check
bash tools/ybx-system-emu/start_eink_qemu11_preview.sh
```

The source checkout (pinned commit, optionally verified by script) was created with Git sparse clone in `~/.local/state/ybx-emulator-upstream/eink-qemu-reference-20261102` and is not used as an unverified build substitution. A preview already using the same container/port/workdir **must not be overwritten**: stop and archive only that private instance or explicitly choose new `YBX_QEMU11_PREVIEW_PORT`, `YBX_QEMU11_QMP_PORT` and `YBX_QEMU11_WORKDIR`; never point it to production 8784.

## Original inkMOD 1.1.10 — release blocker

The exact original 16MiB `inkmod-v1.1.10-x4pro-flash-16m.bin` fails **at very early boot** on upstream 0.9.2 with:

`Guru Meditation Error: Core 1 panic'ed (LoadProhibited)`, PC **`0x4204541c`**, exception address **`0x000060f0`**, backtrace starting `0x42045419` and `0x420465b9`. QEMU reports subsequent `Unhandled debug exception`, another reset. **Reproduced both with an active `-nic user,model=esp32_wifi` and with `-nic none`**; it is NOT specific to user-mode NAT or the new virtual AP. The original 9.2-based YBX kernel already had a different working RTC EXT1/Power acceptance for inkMOD and must stay available. Until the crash is traced and fixed on QEMU 11.1.2 (or explicitly excluded after a user decision), **do not silently update live 8784**.

The QEMU machine `-M x4pro,panel=uc8179`, RTC/flash mapping and early PSRAM/co-processor setup are potential investigation targets, not established root causes. Avoid vendor-firmware patching as a shortcut. Also verify the X4 Pro stock firmware, input/power, real SD FAT integrity, RTC deep sleep, and NAND/SD guests on QEMU11 separately; a Home screenshot alone is not complete acceptance.

## Migration gates / next changes

1. Keep upstream commit and released binary SHA reproducible and separate from old branch; decide whether to fork `serialx/eink-qemu` under the YBX org with full license/attribution or maintain independent pinned upstream plus a minimal ESP32-S3 extension layer.
2. Reproduce and isolate `inkMOD 1.1.10` Core1 LoadProhibited at `0x4204541c`; symbolicate if the unmodified ELF can be obtained, inspect MMU cache/PSRAM startup against QEMU11. No conjectural hardware fixes.
3. Port our browser/session semantics to QEMU11 QMP (screendump, input-send-event, guest UART, persistent SD/Flash and firmware picker), retaining secure bind and authentication for eventual LAN access. Current preview already establishes screen/touch baseline; it does not replace all old `ybx-*` telemetry.
4. Run comparative original-firmware acceptance and copy-on-write SD/FAT checks (original stock, CrossPoint 1.6.5/2.0.0rc, inkMOD, GT911, e-ink UC8279, RTC EXT1, Wi-Fi scan/assoc/DHCP/HTTP) and a controlled LAN security review.
5. Only after new QEMU11 is at parity and has a 8784 rollback image/source plan switch the stable bridge. Keep old 32-patch fork as a historical migration/rollback archive; do not continue adding independent 9.2 Wi-Fi MAC implementations to it.
