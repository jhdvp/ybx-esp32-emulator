# 0102 — Browser Power GPIO3, bounded input and source composition acceptance

Date: 2026-11-01. Branch `feat/browser-power-button-20261101`. Stable browser **8784 remains unchanged**; experimental bridge is locally bound to `127.0.0.1:8794` on the developer Mac and its private QEMU tree is `~/.local/state/ybx-emulator-upstream/qemu-power-button-20261101`.

## User-facing behavior

An explicitly enabled **Hold Power** button appears beside Refresh and Restart in the X4 Pro browser only when QEMU exposes `ybx-x4pro-power-button-pressed` (Boolean QOM). It is visibly labelled **virtual**, with no claim that a physical reader, power draw or hardware button is tested. The default stable server does not offer this action. Pointer down/up/cancel, lost capture, keyboard Space/Enter, window blur, page visibility change and page unload all attempt a release.

The backend is authoritative: `POST /api/power` accepts *only* `application/json` `{"pressed":true|false}`, with a 64-byte body cap and a strict Boolean. The QMP call writes **only** `qom-set /machine/soc ybx-x4pro-power-button-pressed`. No direct RTC reset or `system_reset` on press. Presses are bound to **1.2 seconds** by a server-owned watchdog; retrying `pressed:true` while held does **not** extend the first deadline. A stale watchdog cannot release a newer press. If QMP release fails, the server stops/relaunches the potentially held guest instead of pretending the button is no longer asserted; reboot, ROM switch, firmware switch and close also clear the active session. On page unload the browser makes a best-effort release request; safety never relies on the browser reporting disconnect.

Security: `YBX_BROWSER_POWER_BUTTON=1` must be explicitly enabled, **and the launcher refuses port 8784**, preserving the audited live core. Private loopback is the default; unprotected Power on a private LAN requires a separate explicit `YBX_BROWSER_ALLOW_UNAUTHENTICATED_POWER_LAN=1` acknowledgement. Never expose public IPv4, wildcard host binding or an untrusted network; the HTTP service currently has **no authentication and no TLS**.

## Live browser acceptance: original inkMOD, not a mock

At `127.0.0.1:8794` the experimental QEMU binary and original guest images were loaded by the normal browser server. The `POST /api/firmware` action selected `inkmod-v1.1.10-x4pro-flash-16m.bin`, SHA-256 `3fb63a3101fd662a3914c836a2c5ef4a571bb89a54eb1af4b1834326007258f0`:

| Stage | HTTP/QEMU evidence |
|---|---|
| Guest not yet awakened | Five successive `/api/state` reads: **0 frames**, `ready:false`, `power.pressed:false` |
| `POST /api/power {"pressed":true}` | HTTP 200, `action:"virtual-power"`, `pressed:true`; no explicit QMP reset |
| 1.85 seconds later | `power.pressed:false` (server watchdog), guest `frames>=2`, `ready:true`, original inkMOD Home SHA `8955f0f2d64e2139ce2c9f8af5a570ddfe4c3871ba4c89949730c1cd2b8e3266` |
| UART (original guest) | `rst:0x5 (DSLEEP)`; no Guru Meditation panic |
| Native GT911 tap `(240,590)` | Settings screen SHA `3f9b3bf8fb06988ad82a346c7703bc89f2404c3a2d225d568a1d4f5f61b2685e` |
| Native GT911 tap `(351,110)` | Controls SHA `6f6c73c8190073cad9f31229313d175a162320826d2c3627807aa874eade07a1` |
| Native GT911 tap `(142,110)` | Reader SHA `9f12ad3a7239587882f44899b8c1a8831275814e172d14b0d0f65f72021f2b50` |

These are actual SSD1677 frame hashes from the guest via HTTP; the visual response cannot be satisfied by an HTML-only mock. The browser's default 8784 guest and QEMU binary were not restarted or replaced. Factory on experimental 8794 also produced four real frames before the firmware switch.

## Source composition / QTest

`verify_x4pro_power_source_composition.py` is an offline, **read-only** validator. It composes the pinned audited 26-patch chain (`febae182e132e4055529be423a818225ebddaa3a`), checks the pinned SHA-256 composition manifest verbatim, then applies six specifically hashed experimental patches in dependency order: `0096`, `0097`, both `0098` files, `0100`, `0101`. It compares all changed and added `hw/` and `include/` sources against the compiled research checkout. **PASS: 31 source files, 0 differences**. Source manifest SHA `1cf6b5a47bcb526934537fc11f5ccc49a26e90c5c1295b1e6b5a95dfaa0b15ff`. It requires binary SHA `7b729fba1ceefdc67a55dc9ddd6d359699e16174a52d741ea243849c35e2accc`; the previous 0101 report cited a pre-fix build SHA and was corrected. Local evidence JSON: `/tmp/ybx-browser-power-0102/composition-verdict.json`.

Compiled QEMU QTests, run again against the accepted experimental binary with `--platform linux/arm64 --network none`, all returned zero:

- `probe_x4pro_power_button_rtc_qtest.py`: GPIO3 → RTCIO3, wrong mask/mode/disabled wake and held-level retention
- `probe_x4pro_rtc_ext1_gpio_qtest.py`: RTC EXT1 register-level control and negative gates
- `probe_x4pro_gt911_short_reset_qtest.py`: GT911 glitch/reset safety
- `probe_x4pro_touch_injection_qtest.py`: real virtual GT911 injection semantics

Full host `unittest` suite with companion CrossPoint checkout: **943 tests total, 933 passed, 10 skipped, exit 0** (final recorded run). Node.js syntax validation of the inlined page JavaScript passed. No private flash/ROM/SD bytes, API credentials or compiled QEMU binaries are committed.

## Remaining before replacing the live QEMU

1. The source composition is independently reproducible and the existing experimental *binary* is verified, but **a fresh clean-room binary rebuild from composed sources has not yet been executed**. This is not a claim of byte-for-byte binary reproducibility for 0101.
2. A complete repeat of the broader EPD lifecycle, long-run SDMMC/FAT card and every Factory / CrossPoint 1.6.5 / 2.0.0rc flow on a newly composed binary is still required. RTC analog mux/EXT0/timer and physical button fidelity remain incomplete.
3. The isolated `8794` service is HTTP over loopback only. Browser interaction from phones on the LAN requires deliberate protected routing or a trusted-network opt-in; no external access is presumed.
4. Keep GitHub issue #8 open for full clean-room build / regression and deliberate promotion. **No automatic port 8784 deployment**.
