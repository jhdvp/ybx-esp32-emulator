# 0112 — inkMOD 1.1.10 / QEMU11: X4 Pro GPIO21 charger STAT

Date: 2026-11-04. Experimental QEMU source patch; stable 8784 and saved guest Flash/SD were not touched.

## Real guest root cause (matching ELF / GDB)

- Reference: serialx/eink-qemu 0.9.2, QEMU 11.1.2, pinned source d1b24a5fbc1bbae7ea31075460f63cd4c989c1dc.
- inkMOD: alpzoloto-sudo/inkmod, tag v1.1.10, pioarduino 55.03.37, x4pro-release, ESP32-S3 N16R8 dio_opi.
- Original unmodified flash SHA-256: 3fb63a3101fd662a3914c836a2c5ef4a571bb89a54eb1af4b1834326007258f0.
- Diagnostic rebuilt flash SHA-256: caf46154c7bf4363bf67cef7c2cda2788f906dc15434820939cc83864a067f95; it differs from original.
- Rebuilt DWARF ELF SHA-256: 98ffc195c84b11354201b205a93c8112f7956efde1b73e0b6bc3224ca8136a85, matching its UART panic prefix.
- A GDB hardware breakpoint hit Core 1 at rebuilt PC 0x420453a8 in GfxRenderer::drawPixel, before the LoadProhibited at 0x000060f0.
- At that point renderer.frameBuffer, display.einkDisplay.frameBuffer0 and display.einkDisplay.frameBuffer were all zero. Runtime bufferSize was 0xBB80, 48000 bytes.
- Guest GDB stack: setup():1065 -> enterDeepSleep():700 -> ActivityManager::goToSleep():337 -> SleepActivity::onEnter():526 -> BaseTheme::drawPopup():788 -> GfxRenderer::drawPixel():700.
- The call at main.cpp:1065 is specifically WakeupReason::AfterUSBPower, reached before setupDisplayAndFonts() at line 1116. This disproves the earlier unconfirmed theory that the renderer only failed because heap allocation ran out.

## Reference source comparison

- inkMOD HalGPIO::getWakeupReason(): POWERON + undefined wake cause + isUsbConnected() -> AfterUSBPower.
- inkMOD X4 Pro uses BatteryMonitor::isCharging() as the USB-power surrogate because a dedicated VBUS detection input is unconfirmed.
- The X4 Pro BoardConfig maps active-HIGH charger STAT to GPIO21; CW2017 itself cannot report charger state.
- Reference QEMU hw/gpio/esp32s3_gpio.c reads undriven GPIO as HIGH (floating input pull-up). Reference x4pro_board_init() left GPIO21 undriven.
- Thus the battery-powered emulator cold boot was misreported as charging and inkMOD rendered the sleep popup before the display had been initialized.

## Isolated source patch

Patch: tools/ybx-system-emu/third_party/eink-qemu-0.9.2-x4pro-charging-gpio21.patch.

- Adds a dedicated active-high charger STAT output on the QEMU X4 Pro virtual input board, physically connected to SoC GPIO21.
- Default charging=false drives GPIO21 LOW. QMP can opt in to simulated charging via qom-set /machine/x4pro-keys charging true.
- Does not patch inkMOD or CrossPoint, create fake RAM, alter Wi-Fi, or modify the old QEMU9 backend.
- This is an isolated build against the original 0.9.2 source pin, not a replacement of the official released binary.

## Verified real guest acceptance

- Isolated Linux ARM64 patched QEMU build succeeded. QEMU binary SHA-256: f527e80c2b2319b49d8776bc9002fa03d82191f6492c879d854cac8a46cc65cd.
- QMP before guest execution: charging=false; GPIO21 transitions LOW -> HIGH -> LOW when toggling charging true and then false. Tested using the actual GPIO input MMIO register at 0x6000403C.
- Original unmodified inkMOD 16MiB flash on a private 128MiB FAT16 SD copy: no Guru Meditation Error across 36 seconds of native boot.
- Genuine native QMP UC8279 framebuffer 480x800 was non-uniform; at 18 and 36 seconds it showed the actual inkMOD Home with Browse Files, Search, Recent Books, File Transfer and Settings.
- Second independent test: native QMP GT911 input at pixel (160,342) activated Browse Files. At 36 seconds the real guest screen showed the SD card file browser, not an HTML imitation.
- Original source Flash and SD SHA-256 values remained unchanged across both tests. Only private guest disk copies were writable.
- Five new host unit tests passed for GPIO input decoder and real P6 framebuffer validation.
- Complete host suite at this stage: 981 tests, 11 skipped by environment, zero failures.

## Remaining migration gates

- Power/RTC passed on the first patched native build in isolated probe power-0121: original inkMOD after real Home, QMP Power held 1.5s, query-status RUNNING to SUSPENDED to RUNNING after second Power. Guest ROM logged rst:0x5 (DSLEEP), Home 480x800 re-rendered with no panic. A premature tap and -no-reboot option caused earlier failed harness attempts.
- CrossPoint 2.0 RC native boot smoke passed on the same patched QEMU build (crosspoint-rc-0120): real 480x800 Home after 36s, correct UC8279 0x68 probe, 8MiB PSRAM telemetry, no panic, original media intact.
- **Outstanding**: new Wi-Fi scan/DHCP/original guest HTTP 200 E2E using patched binary, and persistent Flash/SD reboots.
- Validate original CrossPoint Wi-Fi scan, DHCP and HTTP 200 over QEMU NAT, plus stock firmware and persistent media across separate restarts.
- Keep the stable 192.168.10.239:8784 and rollback untouched until ALL parity checks pass.
- Separately report an inkMOD firmware bug: genuine cold USB-charging boot also enters the sleep renderer before display init. This must not be hidden by changing emulator charging semantics.

Native reproducible probe: tools/ybx-system-emu/probe_einkqemu11_inkmod_boot.py. It refuses an existing output directory, copies source images, checks charger QMP/MMIO, captures the genuine frame, and rejects guest panic and changed source media.
## Additional regression and outstanding Power gate

- Original CrossPoint 2.0 RC (not modified) also booted on this same patched QEMU, generated genuine 480x800 frames and did not panic over the 32-second bounded replay. Original source Flash/SD hashes matched pre-run.
- Full host suite in the branch: 981 tests, 11 skipped, 0 failures (970 passed).
- First original inkMOD Power test (0.85s hold) **did not** reach a confirmed QEMU suspended status within its deadline. This does NOT prove RTC EXT1 support yet. A longer 3.2s key-hold test was started separately; do not promote Power until a verified sleep->wake->Home cycle and SD integrity are shown.
- Native Wi-Fi/DHCP/HTTP 200 has prior evidence on the **unpatched** QEMU11 reference and must be repeated against this patched SHA before production promotion.
- The test outputs and guest media live outside Git; no proprietary flash or SD image is committed.

## Runtime packaging caution

The first patched build had optional runtime dependency on libsndio.so.7 absent in ybx-eink-qemu-runner:20261102, though it ran correctly in ybx-eink-qemu-builder:20261102. A second build is configured with --disable-sndio. Its SHA and original guest tests must be checked separately before promotion. Keep stable 8784 unchanged.

## Final runnable build, separate from first builder-only build

- Reconfigured isolated serialx 0.9.2 source with --disable-sndio.
- Final binary SHA-256: 220f668cc4d0c093fbf292e962cd8c7e92801d75392356f0afc015b661588aef.
- Runtime link check: no missing shared libraries in ybx-eink-qemu-runner:20261102; --version = QEMU 11.1.2.
- Copied final binary to local pinned test artifacts; first build was not used for final acceptance.

### Final real guest tests

- Original unmodified inkMOD 1.1.10: 36-second real Home frame, native GT911 Browse Files tap changes guest frame, no panic, input source Flash/SD unchanged.
- Original inkMOD GPIO3 Power: RUNNING -> SUSPENDED -> RUNNING on RTC EXT1 wake; original firmware returns to native Home, no panic, unchanged source Flash/SD.
- CrossPoint 2.0 RC original: actual GT911 Home -> File Transfer -> Join a Network. Three scanned virtual SSIDs; associated PICSimLabWifi with DHCP address 10.0.2.15 and original firmware QR. Container-local NAT forwarded genuine guest HTTP 200, 2103 bytes. The rule was removed.
- Persistence: the same private writable Flash and 128MiB FAT16 SD were used for the second QEMU process without re-creating them; real guest remounted SDMMC and navigated to its file list via native GT911 after reboot, no panic. Original source media remained read-only.
- Stock 1.6.14 RC fixture (private QA Flash and SD): real 480x800 Home on final QEMU, no panic, original fixture media unchanged. **This is a boot-only smoke**, not a new claim of QR pairing, authentication, catalogs or quote sync.
- Host regression suite: 981 tests, 11 skipped, zero failures.

Machine-readable non-sensitive evidence is in evidence/inkmod-qemu11-gpio21/acceptance-20261104.json. Raw firmware, NVS, guest SD and writable image copies remain private local artifacts.

### Promotion boundary

Stable 8784 and its rollback remain in place. This confirms the corrected native QEMU11 engine can run the tested firmware scenarios, but not that *every* old YBX QOM/telemetry/browser capability has been ported. Switch the default only after a separate browser/session and downstream integration comparison, including ongoing plugin/KoSync and Stock quote-sync gates.
