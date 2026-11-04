# 0113 — QEMU11 GPIO21 candidate: browser, real Wi-Fi/HTTP and persistent guest media

Date: 2026-11-04. This is an **isolated** candidate, not the replacement for stable 192.168.10.239:8784.

## Engine provenance and opt-in safety

- Reference source serialx/eink-qemu 0.9.2 pinned at d1b24a5fbc1bbae7ea31075460f63cd4c989c1dc.
- Approved GPIO21 candidate binaries: first isolated build f527e80c2b2319b49d8776bc9002fa03d82191f6492c879d854cac8a46cc65cd; independently rebuilt source candidate 220f668cc4d0c093fbf292e962cd8c7e92801d75392356f0afc015b661588aef.
- Original official QEMU11 SHA 8fce2150c091ceb7b644ac7603169620e9fc171ebaa37bed8aece2a457dcbd6a is still the only accepted SHA in the default browser mode. Candidate opt-in requires one of the two exact experimental SHAs; unknown or mismatched binaries are refused.
- The dedicated launcher start_einkqemu11_charger_candidate.sh defaults to localhost 127.0.0.1:8814, refuses port 8784, mounts ROM and source firmware and SD read-only, with private guest flash/SD writable only in a separate state directory.
- The experimental browser explicitly labels GPIO21 candidate variant in the UI and reports engine variant + binary SHA-256 via /api/state. The upstream-official browser and launcher remain unchanged.

## Repeated native guest verification

- Original unmodified inkMOD v1.1.10 SHA 3fb63a3101fd662a3914c836a2c5ef4a571bb89a54eb1af4b1834326007258f0 boots both the initial candidate and independently rebuilt 220f66 candidate with active-low battery GPIO21. No panic, genuine 480x800 Home.
- Genuine GT911 taps on original inkMOD moved Home -> Browse Files -> SD card browser. Verified native frame change.
- Native Power (without incorrect no-reboot QEMU flag) reached running -> suspended -> running on original inkMOD. UART independently recorded rst:0x5 (DSLEEP), not merely a QMP property transition. Original Flash and SD preserved.
- Original CrossPoint 2.0.0rc switches into the experimental browser via genuine firmware picker without changing the original ROM source; native Home frame captured.

## Native Wi-Fi/DHCP/HTTP — patched 220f668c build

- Real GUI path: Home -> File Transfer -> Join a Network -> scan 3 APs (PICSimLabWifi, Espressif, MasseyWifi) -> tap PICSimLabWifi.
- Guest screen independently displayed connected network PICSimLabWifi and URL http://10.0.2.15/ (or crosspoint.local), indicating guest DHCP.
- Temporary, container-local QEMU user-NAT forwarding tcp:127.0.0.1:18082-:80 resulted in actual HTTP 200, Content-Type text/html and 2103 response bytes at first attempt. Forward removal returned success. No host-LAN port for the guest was published.
- Local evidence in acceptance-eink11-20261102/inkmod-charge-0117/browser-wifi-8814: crosspoint-home.png, crosspoint-filetransfer.png, crosspoint-networks-after.png, crosspoint-dhcp.png, native-http-200.json.

## Actual persistent Flash/SD verification

- Isolated browser first saved selected CrossPoint firmware and wrote only its private guest disks under ~/.local/state/ybx-einkqemu11-charger-preview-8814.
- After stopping the **experimental** Docker container, both private guest media SHA-256 values were recorded: flash e977de4af188fb8e34e330f055cdddcf2f2f3b486a3f6b7a39d07835bddea47a; SD bf99a32675b9b3c1ce6ee6db1c58d3d26a4a02d8a5291af7c40e4091eb1f40a0.
- Container was recreated on the same private persistence volume. The server's initial guest media SHA values **exactly matched the previously recorded stopped-disk hashes**, and selected firmware remained CrossPoint; no implicit reset or replacement.
- Browser returned ready=true with the verified candidate SHA 220f668c. Local evidence persist-before-recreate.sha256 and persist-recreate-verdict.json.

## Release gate

This is not proof of complete physical equivalence. Continue hardware/peripheral smoke for other panel variants, long-run FAT recovery and rollback snapshots before switching production 8784. The old 32-patch QEMU remains the known-good fallback; there is no authorized blind cutover here.
