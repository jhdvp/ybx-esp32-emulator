# Security

This is an experimental firmware emulator and development tool, not an internet-facing service.

- The HTTP control API has **no authentication**. Keep `YBX_BROWSER_BIND_ADDRESS=127.0.0.1` unless you understand and trust your LAN. Never expose it through public port forwarding.
- Use guest network isolation when analyzing untrusted firmware or cloud credentials.
- Never commit factory firmware, private device keys, production tokens, or memory dumps containing secrets.
- Treat QEMU and emulated firmware as untrusted inputs; keep host and Docker up to date.
- For a security issue, open a private GitHub vulnerability report rather than a public issue, if available.
