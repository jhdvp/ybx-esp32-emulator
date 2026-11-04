#!/usr/bin/env bash
# Opt-in native QEMU11 GPIO21-candidate browser. Never touches the stable 8784.
set -euo pipefail

PORT="${YBX_EINK11_CANDIDATE_PORT:-8814}"
WIFI="${YBX_EINK11_CANDIDATE_WIFI:-0}"
POWER="${YBX_EINK11_CANDIDATE_POWER:-0}"
ROOT="${YBX_EINK11_CANDIDATE_ROOT:-$HOME/.local/state/ybx-emulator-upstream/eink-qemu-charger-0117}"
REFERENCE="${YBX_EINK11_REFERENCE_ROOT:-$HOME/Projects/Ybx/artifacts/ybx-esp32-system-emulation-20261029/reference-eink-qemu-20261102/linux/qemu}"
FIRMWARE_DIR="${YBX_EINK11_FIRMWARE_DIR:-$HOME/Projects/Ybx/artifacts/ybx-esp32-system-emulation-20261029/firmware}"
FIRMWARE_NAME="${YBX_EINK11_CANDIDATE_FIRMWARE_NAME:-inkmod-v1.1.10-x4pro-flash-16m.bin}"
SD="${YBX_EINK11_CANDIDATE_SD:-$HOME/Projects/Ybx/artifacts/ybx-esp32-system-emulation-20261029/qa-20261029/ybx-test-sd-fat16-mbr-pristine.img}"
PERSIST="${YBX_EINK11_CANDIDATE_PERSIST:-$HOME/.local/state/ybx-einkqemu11-charger-preview-$PORT}"
TOOLS="$(cd "$(dirname "$0")" && pwd)"
BINARY="${YBX_EINK11_CANDIDATE_BINARY:-$ROOT/build-charge/qemu-system-xtensa}"
BINARY_DIR="$(dirname "$BINARY")"
BINARY_NAME="$(basename "$BINARY")"
EXPECTED_0117="f527e80c2b2319b49d8776bc9002fa03d82191f6492c879d854cac8a46cc65cd"
EXPECTED_0123="220f668cc4d0c093fbf292e962cd8c7e92801d75392356f0afc015b661588aef"
IMAGE="ybx-eink-qemu-builder:20261102"
NAME="ybx-einkqemu11-charger-preview-$PORT"

die() { echo "charger QEMU11 preview: $*" >&2; exit 2; }
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1024 && PORT <= 65535 && PORT != 8784 )) || die "invalid port or protected 8784"
[[ "$WIFI" == 0 || "$WIFI" == 1 ]] || die "YBX_EINK11_CANDIDATE_WIFI must be 0 or 1"
[[ "$POWER" == 0 || "$POWER" == 1 ]] || die "YBX_EINK11_CANDIDATE_POWER must be 0 or 1"
[[ "$FIRMWARE_NAME" == "$(basename "$FIRMWARE_NAME")" ]] || die "firmware basename required"
[[ "$PERSIST" == /* && ! -L "$PERSIST" ]] || die "persistence path must be absolute, not a symlink"
[[ -x "$BINARY" ]] || die "patched executable missing: $BINARY"
[[ -f "$REFERENCE/share/qemu/esp32s3_rev0_rom.bin" ]] || die "reference ESP32-S3 ROM not found"
[[ -f "$FIRMWARE_DIR/$FIRMWARE_NAME" && -f "$SD" ]] || die "source firmware or SD missing"
[[ "$(wc -c < "$FIRMWARE_DIR/$FIRMWARE_NAME" | tr -d ' ')" == 16777216 ]] || die "firmware must be full 16MiB"
[[ "$(wc -c < "$SD" | tr -d ' ')" == 134217728 ]] || die "SD must be full 128MiB"
ACTUAL="$(shasum -a 256 "$BINARY" | awk '{print $1}')"
[[ "$ACTUAL" == "$EXPECTED_0117" || "$ACTUAL" == "$EXPECTED_0123" ]] || die "unverified candidate binary SHA"
if [[ "${1:-}" == "--check" ]]; then
  echo "Candidate QEMU11 SHA verified: $ACTUAL"
  echo "Guest media read-only sources verified; protected port 8784 is not used"
  exit 0
fi
[[ "$#" == 0 ]] || die "unknown option (only --check supported)"
docker image inspect "$IMAGE" >/dev/null 2>&1 || die "offline builder image missing"
docker container inspect "$NAME" >/dev/null 2>&1 && die "existing container $NAME must not be replaced"
mkdir -p "$PERSIST"
EXTRA=()
if [[ "$WIFI" == 1 ]]; then EXTRA+=(--wifi); fi
if [[ "$POWER" == 1 ]]; then EXTRA+=(--enable-power); fi
docker run --rm -d --platform linux/arm64 --network bridge \
  --name "$NAME" -p "127.0.0.1:$PORT:$PORT" \
  -v "$BINARY_DIR:/patched:ro" -v "$REFERENCE/share/qemu:/roms:ro" \
  -v "$FIRMWARE_DIR:/fw:ro" -v "$SD:/input-sd.img:ro" \
  -v "$TOOLS:/tools:ro" -v "$PERSIST:/persist:rw" \
  "$IMAGE" python3 -u /tools/serve_einkqemu11_browser.py \
    --qemu "/patched/$BINARY_NAME" --charger-candidate \
    --rom-dir /roms --flash "/fw/$FIRMWARE_NAME" --sd /input-sd.img \
    --firmware-dir /fw --work-dir /persist \
    --host 0.0.0.0 --container-bind --port "$PORT" \
    "${EXTRA[@]}"
echo "Experimental QEMU11 GPIO21 candidate: http://127.0.0.1:$PORT/"
echo "Persistent private media: $PERSIST"
echo "Stop: docker stop $NAME"
