#!/bin/sh
# Explicit opt-in: add tested read-only diagnostics to an ALREADY 0042-patched
# isolated ESP32-S3 QEMU source checkout. Does not touch firmware or browser.
set -eu

if [ "$#" -ne 2 ] || { [ "$1" != "--check" ] && [ "$1" != "--apply" ]; }; then
  echo "Usage: $0 --check|--apply /absolute/path/to/isolated-0042-qemu" >&2
  exit 2
fi
MODE="$1"
QEMU_ROOT="$2"
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
test -f "$QEMU_ROOT/hw/display/ybx_ssd1677.c" || {
  echo "QEMU does not contain the opt-in X4 Pro SSD1677 model; refuse" >&2
  exit 2
}
test -f "$QEMU_ROOT/hw/xtensa/esp32s3_intc.c" || {
  echo "Not an ESP32-S3 QEMU source checkout; refuse" >&2
  exit 2
}
grep -q 'GPIO_PCPU_INT' "$QEMU_ROOT/hw/gpio/esp32_gpio.c" || {
  echo "Requires proven 0042 GPIO PCPU_INT prerequisite; refuse" >&2
  exit 2
}
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT HUP INT TERM
# Agent's historical 0043 patch contains a prose preamble. Only the unified
# diff is a git-apply payload.
sed -n '/^diff --git /,$p' "$HERE/experiments/0043-s3-optin-crosscore-ipi-counters.patch" > "$TMP"
test -s "$TMP"
EPD="$HERE/experiments/0037-x4pro-epd-busy-window-edge-telemetry.patch"

for file in "$TMP" "$EPD"; do
  if [ "$file" = "$TMP" ]; then label="0043-crosscore-IPI"; else label="0037-EPD-BUSY"; fi
  if git -C "$QEMU_ROOT" apply --reverse --check "$file" 2>/dev/null; then
    echo "Already applied: ${label}"
  elif git -C "$QEMU_ROOT" apply --check "$file"; then
    if [ "$MODE" = "--apply" ]; then
      git -C "$QEMU_ROOT" apply "$file"
      echo "Applied: ${label}"
    else
      echo "Compatible (not applied): ${label}"
    fi
  else
    echo "Cannot safely apply ${label}; source differs" >&2
    exit 3
  fi
done
if [ "$MODE" = "--apply" ]; then
  echo "Rebuild qemu-system-xtensa separately; never replace a live image without native guest QA."
fi
