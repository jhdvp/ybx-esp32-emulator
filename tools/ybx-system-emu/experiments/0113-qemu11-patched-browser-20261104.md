# 0113 — patched QEMU11 native browser, localhost-only persistent session

Date: 2026-11-04. This is an **opt-in experimental browser**, not the stable emulator replacement.

## Firmware and engine provenance

- Verified upstream: serialx/eink-qemu 0.9.2 / QEMU 11.1.2, source commit d1b24a5fbc1bbae7ea31075460f63cd4c989c1dc.
- YBX hardware fix already merged as PR #22: virtual X4 Pro charger STAT connected to active-high GPIO21, initially LOW.
- Patched runtime build SHA-256: 220f668cc4d0c093fbf292e962cd8c7e92801d75392356f0afc015b661588aef, compiled with --disable-sndio, checked in the original ybx-eink-qemu-runner:20261102 for no missing libraries.
- Browser server permits only the exact audited upstream release binary SHA and this exact accepted patched SHA. Arbitrary binaries still fail closed. Default official release remains supported.

## Experimental browser acceptance

- Added start_eink_qemu11_patched_browser.sh. First launch creates private persistent writable Flash/SD; explicit --reuse restarts the **same** media; no original firmware image or SD input is writable in Docker.
- Refuses an already existing same-name Docker container and does not stop/restart other processes. Initial port 8912 was occupied by an unrelated process; we did not alter it, and used 8913.
- Uses Docker bridge networking for browser port publishing, restricted to host 127.0.0.1:8913; host LAN is not exposed. Initial --network none run could not publish the HTTP port, and was replaced without modifying persisted media.
- Browser URL on the Mac: http://127.0.0.1:8913/ . Container: ybx-eink-qemu11-patched-8913. Persistent private media: acceptance-eink11-20261102/inkmod-charge-0117/browser-8913.
- HTTP GET /api/state reports guest_booted=true, ready=true, native 480x800, original inkMOD v1.1.10 SHA 3fb63a31... and persistent=true.
- HTTP GET /screen.png => 200, 480x800 PNG.
- HTTP POST /api/tap via real native QMP GT911 changed the framebuffer SHA from 04413e4c... (Home) to 8591d388... (SD browser). QEMU remained booted.
- Browser firmware picker and persistent flash/SD remain in separate experimental backend; production 8784, its ports and rollback were left untouched.

## Non-claims and remaining gates

This does not establish that every legacy YBX QOM telemetry feature is present in upstream QEMU11. It also does not replace the dedicated KoSync, plugin two-way citation, stock device-code pairing, and custom YBX firmware UI tests.

Do not switch the default engine, expose the unauthenticated experimental browser on a LAN IP, or remove the 8784 rollback without separately verifying those integrations.
