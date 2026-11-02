# YBX QEMU 11 — guest HTTP smoke check

Read-only diagnostic for an isolated eink-qemu 11.1.2 X4 Pro session.

**Important:** CrossPoint starts its HTTP server inside the **File Transfer**
activity. If the reader displays native YBX shelves instead, an HTTP
connection reset/close is not sufficient evidence of a network regression.

From the emulator repository root, inspect the bridge and optional HTTP
reachability (inconclusive if server mode is not asserted):

```sh
python3 tools/ybx-system-emu/probe_ybx_guest_http.py \
  --bridge-port 8820 --container ybx-eink-qemu11-8820 --guest-port 18080
```

Only when the reader is visibly in **File Transfer**, run an HTTP
acceptance check that expects the endpoints to respond:

```sh
python3 tools/ybx-system-emu/probe_ybx_guest_http.py --expect-file-transfer
```

The preview bridge is reachable at Mac loopback 8820; the guest HTTP
port 18080 is forwarded **inside the container**, not published on Mac.
The probe reads `/api/state`, the screenshot header, `/api/plugins`
and the public `plugin.js`. It never reads guest response bodies,
accesses tokens, taps the screen or changes SD files.

Exit 0 means both plugin endpoints answered HTTP 200, QMP is running and
the screen is 480×800. Exit 1 means the asserted File Transfer
precondition was not met by the results. Exit 2 indicates an **inconclusive**
probe outside a confirmed File Transfer session. A running QMP does not
prove functioning Wi-Fi, DHCP, HTTPS or device-code authentication.

The current isolated SD has a syntactically valid `token.json` with a
nonempty access-token field (verified without printing the secret). The
native CrossPoint catalog intentionally bypasses QR enrollment when the
token file exists. For a fresh QR acceptance test, use a **disposable copy**
of the SD without the synthetic test token; preserve the original session.

Out of scope: OAuth device-code completion, real libraries, `/api/fetch`
(which writes to SD), sleep-card upgrades, reboot and physical X4 Pro.
Keep credentials and one-time pairing codes out of logs and Git.

Tracking: [E2E QA #9](https://github.com/jhdvp/ybx-crosspoint-plugin/issues/9),
[TLS download safety #10](https://github.com/jhdvp/ybx-crosspoint-plugin/issues/10).
