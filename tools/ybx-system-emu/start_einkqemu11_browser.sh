#!/usr/bin/env bash
# Isolated preview: eink-qemu 0.9.2 / QEMU 11.1.2. Never replace port 8784.
set -euo pipefail

PORT="${YBX_EINK11_PORT:-8806}"
WIFI="${YBX_EINK11_WIFI:-0}"
POWER="${YBX_EINK11_POWER:-0}"
ENGINE="${YBX_EINK11_ROOT:-$HOME/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/reference-eink-qemu-20261102/linux/qemu}"
QA="${YBX_EINK11_QA_DIR:-$HOME/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/qa-20261029}"
FIRMWARE="${YBX_EINK11_FIRMWARE_DIR:-$HOME/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/firmware}"
PERSIST="${YBX_EINK11_PERSIST_DIR:-$HOME/.local/state/ybx-einkqemu11-media/preview-$PORT}"
TOOLS="$(cd "$(dirname "$0")" && pwd)"
BINARY="$ENGINE/bin/qemu-system-xtensa"
IMAGE="ybx-eink-qemu-runtime:0.9.2"
EXPECTED="8fce2150c091ceb7b644ac7603169620e9fc171ebaa37bed8aece2a457dcbd6a"
die() { echo "eink11 migration: $*" >&2; exit 2; }

[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1024 && PORT <= 65535 && PORT != 8784 )) ||
  die "invalid isolated port (8784 is reserved)"
[[ "$WIFI" == 0 || "$WIFI" == 1 ]] || die "YBX_EINK11_WIFI must be 0 or 1"
[[ "$POWER" == 0 || "$POWER" == 1 ]] || die "YBX_EINK11_POWER must be 0 or 1"
[[ -f "$BINARY" && -d "$ENGINE/share/qemu" ]] || die "QEMU 11.1.2 release missing"
[[ -f "$QA/s3-flash-complete-rom-test.bin" &&
   -f "$QA/ybx-test-sd-fat16-mbr-pristine.img" ]] || die "QA media missing"
[[ -d "$FIRMWARE" ]] || die "firmware directory missing"
[[ "$PERSIST" == /* && ! -L "$PERSIST" ]] || die "persistent media must have an absolute non-symlink path"
[[ "$(shasum -a 256 "$BINARY" | awk '{print $1}')" == "$EXPECTED" ]] ||
  die "QEMU 11.1.2 binary SHA mismatch"
docker image inspect "$IMAGE" >/dev/null 2>&1 || die "offline runtime image missing"
NAME="ybx-einkqemu11-browser-$PORT"
if docker container inspect "$NAME" >/dev/null 2>&1; then
  die "$NAME already exists; refusing to replace another process"
fi
mkdir -p "$PERSIST"
WIFI_EXTRA=""
if [[ "$WIFI" == 1 ]]; then WIFI_EXTRA="--wifi"; fi
POWER_EXTRA=""
if [[ "$POWER" == 1 ]]; then POWER_EXTRA="--enable-power"; fi
echo "Preview: http://127.0.0.1:$PORT/"
echo "QEMU11 with guest Wi-Fi NAT=$WIFI and persistent disks=$PERSIST"
docker run --rm -d --platform linux/arm64 --network bridge \
  --name "$NAME" -p "127.0.0.1:$PORT:$PORT" \
  -v "$ENGINE:/engine:ro" -v "$TOOLS:/tools:ro" \
  -v "$QA:/qa:ro" -v "$FIRMWARE:/fw:ro" \
  -v "$PERSIST:/persist:rw" \
  "$IMAGE" python3 /tools/serve_einkqemu11_browser.py \
    --qemu /engine/bin/qemu-system-xtensa \
    --rom-dir /engine/share/qemu \
    --flash /qa/s3-flash-complete-rom-test.bin \
    --sd /qa/ybx-test-sd-fat16-mbr-pristine.img \
    --firmware-dir /fw --work-dir /persist \
    --host 0.0.0.0 --container-bind --port "$PORT" \
    ${WIFI_EXTRA:+--wifi} \
    ${POWER_EXTRA:+--enable-power}
