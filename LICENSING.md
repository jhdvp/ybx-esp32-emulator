# Licensing

This repository mixes original host-side tooling with patches against QEMU.
The two have different licenses.

## MIT — original project work

Everything authored for this project is MIT-licensed under [`LICENSE`](LICENSE):
the browser bridge, probes, tests, analysis scripts, documentation, screenshots,
README artwork and the experiment reports.

## GPL-2.0 — QEMU-derived patch files

These files are unified diffs against QEMU (the Espressif QEMU fork is GPL-2.0),
so they are derivative works and are distributed under **GPL-2.0**:

- `tools/ybx-system-emu/patches/*.patch`
- `tools/ybx-system-emu/experiments/*.patch`

License text: <https://www.gnu.org/licenses/old-licenses/gpl-2.0.html>

The `.md`, `.gdb`, `.json` and standalone `.c` research artifacts that describe
or model behaviour are original work and remain MIT, unless a file states
otherwise.

## What is not distributed here

No QEMU source tree, QEMU binary, ESP32 factory firmware, ROM image, device key
or vendor SDK is redistributed. `tools/ybx-system-emu/Dockerfile.qemu` builds
QEMU from upstream at a pinned revision; the resulting binary is GPL-2.0.

## If you reuse something

- Reusing host tooling, tests or docs: follow MIT, keep the copyright notice.
- Reusing or shipping a QEMU patch: follow GPL-2.0.
- Distributing a built emulator binary: follow GPL-2.0 and QEMU's own
  attribution requirements.

This document is a practical summary, not legal advice.
