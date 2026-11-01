#!/usr/bin/env bash
# Start a real factory X4 Pro QEMU guest with an interactive browser display.
# Default HTTP binding: Mac loopback only. Explicitly opt in to a LAN IPv4
# with YBX_BROWSER_BIND_ADDRESS=192.168.x.y (NO AUTH; trusted LAN ONLY).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QEMU_ROOT="${YBX_QEMU_ROOT:-$HOME/.local/state/ybx-emulator-upstream/qemu-rominit-20261029}"
QA_ROOT="${YBX_QA_DIR:-$HOME/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/qa-20261029}"
PORT="${YBX_BROWSER_PORT:-8784}"
BIND_IP="${YBX_BROWSER_BIND_ADDRESS:-127.0.0.1}"
POWER_FLAG="${YBX_BROWSER_POWER_BUTTON:-0}"
if [[ "$POWER_FLAG" != "0" && "$POWER_FLAG" != "1" ]]; then
  echo "YBX_BROWSER_POWER_BUTTON must be 0 or 1" >&2
  exit 2
fi
# Never silently opt an experimental binary into the established 8784 slot.
if [[ "$POWER_FLAG" == "1" && "$PORT" == "8784" ]]; then
  echo "Refusing experimental Power on stable browser port 8784; choose an isolated port" >&2
  exit 2
fi
POWER_EXTRA=""
if [[ "$POWER_FLAG" == "1" ]]; then
  POWER_EXTRA=1
  echo "EXPERIMENTAL X4 Pro POWER GPIO3 enabled on this instance only." >&2
fi
# Folder of candidate ROM files for the in-page picker. The bridge only READS
# it (mounted :ro); the operator drops his own .bin files here on the Mac.
ROM_DIR="${YBX_ROM_DIR:-${QA_ROOT%/*}/roms}"
# Folder of candidate firmware images for the in-page picker. The bridge only
# READS it (mounted :ro); the operator drops his own .bin files here on the Mac.
FW_DIR="${YBX_FIRMWARE_DIR:-${QA_ROOT%/*}/firmware}"
CONTAINER="ybx-x4pro-browser-${PORT}"
IMAGE="ybx-qemu-builder:20261029"
if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1024 || PORT > 65535 )); then
  echo "Invalid port; use 1024..65535" >&2
  exit 2
fi
if ! python3 -c 'import ipaddress,sys; ip=ipaddress.ip_address(sys.argv[1]); ok=ip.version==4 and not ip.is_unspecified and not ip.is_link_local and (ip.is_loopback or ip.is_private); sys.exit(0 if ok else 1)' "$BIND_IP"; then
  echo "Refusing non-private or invalid HTTP bind address: $BIND_IP" >&2
  echo "Only IPv4 loopback (127.0.0.1) or private LAN ranges are allowed;" >&2
  echo "public, link-local and IPv6 binds are rejected." >&2
  exit 2
fi
if [[ "$POWER_FLAG" == "1" && "$BIND_IP" != "127.0.0.1" &&
      "${YBX_BROWSER_ALLOW_UNAUTHENTICATED_POWER_LAN:-0}" != "1" ]]; then
  echo "Experimental Power requires loopback; to expose on a trusted LAN, explicitly set" >&2
  echo "YBX_BROWSER_ALLOW_UNAUTHENTICATED_POWER_LAN=1 (NO AUTH / NO TLS)" >&2
  exit 2
fi
if [[ "$BIND_IP" != "127.0.0.1" ]]; then
  {
    echo "############################################################"
    echo "# WARNING: exposing the YBX browser bridge on $BIND_IP:$PORT"
    echo "# This HTTP bridge has NO authentication and NO TLS."
    echo "# Anyone who can route to this address can watch the screen,"
    echo "# inject GT911 taps and reboot the virtual reader."
    echo "# Prefer the default 127.0.0.1, or forward over SSH:"
    echo "#   ssh -L $PORT:127.0.0.1:$PORT <host>"
    echo "############################################################"
  } >&2
fi
for file in "$QEMU_ROOT/build-ybx/qemu-system-xtensa" \
            "$QA_ROOT/s3-full-rom-generator-test.bin" \
            "$QA_ROOT/s3-flash-complete-rom-test.bin" \
            "$QA_ROOT/ybx-test-sd-fat16-mbr-pristine.img"; do
  [[ -f "$file" ]] || { echo "Missing required experimental file: $file" >&2; exit 2; }
done
[[ -d "$ROM_DIR" ]] || { echo "Creating ROM folder for the picker: $ROM_DIR" >&2; mkdir -p "$ROM_DIR"; }
[[ -d "$FW_DIR" ]] || { echo "Creating firmware folder for the picker: $FW_DIR" >&2; mkdir -p "$FW_DIR"; }
# Seed the factory flash once so "switch back to factory" is always possible
# from the picker. The bridge server itself only reads FW_DIR (mounted :ro).
if [[ ! -e "$FW_DIR/s3-flash-complete-rom-test.bin" ]]; then
  cp "$QA_ROOT/s3-flash-complete-rom-test.bin" "$FW_DIR/s3-flash-complete-rom-test.bin"
fi
# Seed the factory ROM once so "switch back" is always possible from the
# picker. The bridge server itself only reads ROM_DIR (mounted :ro).
if [[ ! -e "$ROM_DIR/s3-full-rom-generator-test.bin" ]]; then
  cp "$QA_ROOT/s3-full-rom-generator-test.bin" "$ROM_DIR/s3-full-rom-generator-test.bin"
fi
if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  if [[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER")" == "true" ]]; then
    echo "Existing running container: $CONTAINER"
    docker ps --filter "name=^/$CONTAINER$" --format '{{.Status}}'
  else
    # A stopped container can retain the name even after `docker stop`.
    # Remove only this emulator container so a fresh guest can start.
    echo "Removing stopped container: $CONTAINER"
    docker rm "$CONTAINER" >/dev/null
  fi
fi
if ! docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  docker run --rm -d --name "$CONTAINER" \
    --platform linux/arm64 --network bridge \
    -p "$BIND_IP:$PORT:$PORT" \
    -v "$QEMU_ROOT:/qemu:ro" \
    -v "$HERE:/tests:ro" \
    -v "$QA_ROOT:/work:ro" \
    -v "$ROM_DIR:/roms:ro" \
    -v "$FW_DIR:/fw:ro" \
    -w /tmp "$IMAGE" \
    python3 -u /tests/serve_x4pro_browser.py \
      --qemu-bin /qemu/build-ybx/qemu-system-xtensa \
      --rom /work/s3-full-rom-generator-test.bin \
      --rom-dir /roms \
      --firmware-dir /fw \
      --flash /work/s3-flash-complete-rom-test.bin \
      --sd-image /work/ybx-test-sd-fat16-mbr-pristine.img \
      --host 0.0.0.0 --port "$PORT" \
      ${POWER_EXTRA:+--enable-power-button}
fi
echo "Open: http://$BIND_IP:$PORT/ (LAN binding has NO authentication; trusted network only)"
echo "ROM folder (read-only for the bridge; drop .bin files here to offer them in the picker): $ROM_DIR"
echo "Firmware folder (read-only for the bridge; drop firmware .bin files here): $FW_DIR"
echo "Stop: docker stop $CONTAINER"
