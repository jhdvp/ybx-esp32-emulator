# YBX QEMU 11 — guest HTTP smoke check

Read-only diagnostic for a separate eink-qemu 11.1.2 X4 Pro session.
A running QMP and visible frame **do not prove** firmware Wi-Fi or HTTP.

From the emulator repository root:

```sh
python3 tools/ybx-system-emu/probe_ybx_guest_http.py \
  --bridge-port 8820 --container ybx-eink-qemu11-8820 --guest-port 18080
```

The preview bridge is reachable at Mac loopback 8820; the guest HTTP
port 18080 is forwarded **inside the container**, not published on Mac.
The probe reads `/api/state`, the screenshot header, `/api/plugins`
and the publicly served `plugin.js`. It does not read response bodies
from the guest, access credentials, tap the screen or modify SD files.

Exit 0 means running QMP, a 480×800 screen, and HTTP 200 responses
for both plugin endpoints. Exit 1 means the JSON report identifies a
failed layer. Neither outcome proves HTTPS or account authorization.

Out of scope: one-time device pairing, real libraries, `/api/fetch`
(which writes to SD), sleep-card upgrade, reboot and physical-device
verification. Keep tokens and pairing codes out of logs and Git.

Tracking: [E2E QA #9](https://github.com/jhdvp/ybx-crosspoint-plugin/issues/9),
[TLS download safety #10](https://github.com/jhdvp/ybx-crosspoint-plugin/issues/10).
