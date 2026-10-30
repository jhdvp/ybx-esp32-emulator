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
CONTAINER="ybx-x4pro-browser-${PORT}"
IMAGE="ybx-qemu-builder:20261029"
if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1024 || PORT > 65535 )); then
  echo "Invalid port; use 1024..65535" >&2
  exit 2
fi
if ! python3 -c 'import ipaddress,sys; ip=ipaddress.ip_address(sys.argv[1]); sys.exit(0 if ip.version == 4 and ip.is_private and not ip.is_unspecified else 1)' "$BIND_IP"; then
  echo "Refusing non-private or invalid HTTP bind address: $BIND_IP" >&2
  exit 2
fi
for file in "$QEMU_ROOT/build-ybx/qemu-system-xtensa" \
            "$QA_ROOT/s3-full-rom-generator-test.bin" \
            "$QA_ROOT/s3-flash-complete-rom-test.bin" \
            "$QA_ROOT/ybx-test-sd-fat16-mbr-pristine.img"; do
  [[ -f "$file" ]] || { echo "Missing required experimental file: $file" >&2; exit 2; }
done
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
    -w /tmp "$IMAGE" \
    python3 -u /tests/serve_x4pro_browser.py \
      --qemu-bin /qemu/build-ybx/qemu-system-xtensa \
      --rom /work/s3-full-rom-generator-test.bin \
      --flash /work/s3-flash-complete-rom-test.bin \
      --sd-image /work/ybx-test-sd-fat16-mbr-pristine.img \
      --host 0.0.0.0 --port "$PORT"
fi
echo "Open: http://$BIND_IP:$PORT/ (LAN binding has NO authentication; trusted network only)"
echo "Stop: docker stop $CONTAINER"
