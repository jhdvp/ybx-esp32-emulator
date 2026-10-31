#!/bin/sh
# Apply tested ESP32 peripheral fixes to a SEPARATE espressif/qemu checkout.
# Never changes FreeInkSDK or production YBX Reader firmware.
set -eu
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
QEMU_ROOT="$HOME/.local/state/ybx-emulator-upstream/qemu"
if [ "$#" -gt 0 ]; then QEMU_ROOT="$1"; fi
git -C "$QEMU_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  echo "Not a QEMU Git checkout: $QEMU_ROOT" >&2; exit 2;
}

# Apply only accepted, verified CPU-peripheral corrections.
# The DRAM initializer experiments under experiments/ are rejected.
for name in 0001-esp32c3-gdma-interrupt-layout.patch 0003-usb-serial-jtag-tx-ready.patch 0005-usb-serial-jtag-output.patch; do
  PATCH="$HERE/patches/$name"
  test -s "$PATCH" || { echo "Missing patch: $PATCH" >&2; exit 2; }
  if git -C "$QEMU_ROOT" apply --reverse --check "$PATCH" 2>/dev/null; then
    echo "Already applied: $name"
  elif git -C "$QEMU_ROOT" apply --check "$PATCH"; then
    git -C "$QEMU_ROOT" apply "$PATCH"
    echo "Applied: $name"
  else
    echo "Cannot apply $name: upstream differs; refusing to overwrite edits" >&2
    exit 3
  fi
done
echo "Rebuild both qemu-system-riscv32 and qemu-system-xtensa after patches."
