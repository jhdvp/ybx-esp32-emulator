#!/usr/bin/env bash
# Start the PINNED official eink-qemu 0.9.2 / upstream QEMU 11.1.2 safely.
# No modifications to the existing stable 8784 instance or its guest media.
set -euo pipefail
TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="${YBX_EINK_QEMU_RELEASE_DIR:-$HOME/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/reference-eink-qemu-20261102}"
UPSTREAM_SRC="${YBX_EINK_QEMU_SOURCE_DIR:-$HOME/.local/state/ybx-emulator-upstream/eink-qemu-reference-20261102}"
BINARY="$BASE/linux/qemu/bin/qemu-system-xtensa"
ARCHIVE="$BASE/eink-qemu-xtensa-softmmu-0.9.2-aarch64-linux-gnu.tar.xz"
QEMU_ROOT="$BASE/linux/qemu"
FW="${YBX_QEMU11_FLASH:-$HOME/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/firmware/crosspoint-2.0.0rc-x4pro-flash-16m.bin}"
SD="${YBX_QEMU11_SD:-$HOME/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/qa-20261029/ybx-test-sd-fat16-mbr-pristine.img}"
PORT="${YBX_QEMU11_PREVIEW_PORT:-8810}"
QMP_PORT="${YBX_QEMU11_QMP_PORT:-8811}"
WORK="${YBX_QEMU11_WORKDIR:-$BASE/preview-$PORT}"
IMAGE="${YBX_QEMU11_DOCKER_IMAGE:-ybx-eink-qemu-runner:20261102}"
NAME="ybx-eink-qemu11-$PORT"
LOCK="$TOOLS/third_party/eink-qemu-0.9.2.lock.json"
if [[ "$PORT" == "$QMP_PORT" || "$PORT" == "8784" || "$QMP_PORT" == "8784" ]]; then
  echo 'Refusing live 8784 or port collision' >&2
  exit 2
fi
# Do not trust a user-controlled shell path/port as an executable or bind value.
python3 - "$LOCK" "$BINARY" "$ARCHIVE" "$UPSTREAM_SRC" "$FW" "$SD" "$PORT" "$QMP_PORT" <<'PY'
import json,hashlib,subprocess,sys
from pathlib import Path
lock,binary,archive,source,flash,sd,port,qmp = sys.argv[1:]
d=json.loads(Path(lock).read_text())
for name in (port,qmp):
    if not name.isdecimal() or not 8800 <= int(name) <= 65534:
        raise SystemExit('Preview ports must be 8800..65534')
for filename,expected in ((binary,d['linux_arm64_extracted_binary_sha256']),
                          (archive,d['linux_arm64_archive_sha256'])):
    path=Path(filename)
    if not path.is_file():
        raise SystemExit('Official eink-qemu release missing: '+filename)
    with path.open('rb') as f:
        actual=hashlib.file_digest(f,'sha256').hexdigest()
    if actual != expected:
        raise SystemExit('eink-qemu official pinned SHA mismatch: '+filename)
if Path(source).is_dir():
    actual=subprocess.check_output(['git','-C',source,'rev-parse','HEAD'],text=True).strip()
    if actual != d['source_commit_main_verified_2026_11_02']:
        raise SystemExit('eink-qemu source ref changed; review upstream before updating')
for filename,size in ((flash,16*1024*1024),(sd,None)):
    path=Path(filename)
    if not path.is_file():
        raise SystemExit('Original input missing: '+filename)
    if size is not None and path.stat().st_size != size:
        raise SystemExit('Not a 16MiB full-flash dump: '+filename)
    if size is None and path.stat().st_size % 512 != 0:
        raise SystemExit('SD image not 512B sector aligned')
print('Pinned official QEMU 11.1.2 / 0.9.2 SHA and input checks PASS')
PY
if [[ "${1:-}" == "--check" ]]; then
  exit 0
fi
[[ "${1:-}" == "" ]] || { echo 'Only --check accepted' >&2; exit 2; }
[[ "$WORK" = /* && ! -e "$WORK" ]] || { echo "Fresh absolute work directory required: $WORK" >&2; exit 2; }
if docker container inspect "$NAME" >/dev/null 2>&1; then
  echo "Container already exists: $NAME (will not stop or replace it)" >&2
  exit 2
fi
docker image inspect "$IMAGE" >/dev/null
if command -v lsof >/dev/null; then
  for p in "$PORT" "$QMP_PORT"; do
    if lsof -nP -iTCP:"$p" -sTCP:LISTEN -t |grep -q .;then
      echo "Port in use: $p" >&2
      exit 2
    fi
  done
fi
mkdir "$WORK"
cp "$FW" "$WORK/flash.bin"
cp "$SD" "$WORK/sd.img"
# Expose only QMP on host loopback. The fake PICSimLabWifi AP uses QEMU user NAT.
# Guest HTTP is reachable only within Docker, never published to the LAN.
docker run -d --name "$NAME" --platform linux/arm64 --network bridge \
  -p "127.0.0.1:$QMP_PORT:4488" \
  -v "$QEMU_ROOT:/ext:ro" -v "$WORK:/out:rw" -w /out \
  "$IMAGE" /ext/bin/qemu-system-xtensa \
  -machine x4pro -accel tcg -icount shift=0,sleep=off \
  -L /ext/share/qemu \
  -drive file=/out/flash.bin,if=mtd,format=raw \
  -drive file=/out/sd.img,if=sd,format=raw \
  -chardev file,id=cdc,path=/out/uart.log -serial null \
  -global driver=misc.esp32s3.usb_serial_jtag,property=chardev,value=cdc \
  -monitor none -display none -qmp tcp:0.0.0.0:4488,server=on,wait=off \
  -nic user,model=esp32_wifi,hostfwd=tcp:127.0.0.1:18080-:80
nohup python3 -u "$TOOLS/eink_qemu11_preview_bridge.py" \
    --workdir "$WORK" --port "$PORT" --qmp-port "$QMP_PORT" \
    > "$WORK/bridge.log" 2>&1 </dev/null &
echo $! > "$WORK/bridge.pid"
echo "eink-qemu QEMU 11.1.2 preview: http://127.0.0.1:$PORT/"
echo "Test data: $WORK; Docker: $NAME; accepted SHA from official release"
echo "Original production 8784 and its media were not modified"
