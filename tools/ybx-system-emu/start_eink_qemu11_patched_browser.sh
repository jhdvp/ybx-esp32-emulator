#!/usr/bin/env bash
# Opt-in patched QEMU11 browser. Never replaces the stable 8784.
set -euo pipefail
TOOLS="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HOME/Projects/Ybx"
ART="$ROOT/artifacts/ybx-esp32-system-emulation-20261029"
BIN="$ART/acceptance-eink11-20261102/inkmod-charge-0117/patched-runtime-0123/qemu-system-xtensa"
ROM="$ART/reference-eink-qemu-20261102/linux/qemu/share/qemu"
FW="$ART/firmware"
FLASH="inkmod-v1.1.10-x4pro-flash-16m.bin"
SD="$ART/acceptance-eink11-20261102/inkmod-0109-rebuilt/repro/guest-sd.img"
PORT="8913"
WORK="$ART/acceptance-eink11-20261102/inkmod-charge-0117/browser-$PORT"
IMAGE="ybx-eink-qemu-runner:20261102"
NAME="ybx-eink-qemu11-patched-$PORT"
MODE=""
if [[ "$#" -gt 1 ]]; then echo "Only one optional argument accepted" >&2; exit 2; fi
if [[ "$#" -gt 0 ]]; then MODE="$1"; fi
case "$MODE" in
  ""|"--check"|"--reuse") ;;
  *) echo "Use: $0 [--check|--reuse]" >&2; exit 2 ;;
esac
python3 - "$BIN" "$ROM" "$FW" "$FLASH" "$SD" "$WORK" <<'PY'
import hashlib, sys
from pathlib import Path
binary,rom,fwdir,flash,sd,work=sys.argv[1:]
sha="220f668cc4d0c093fbf292e962cd8c7e92801d75392356f0afc015b661588aef"
for p in [Path(binary),Path(rom)/"esp32s3_rev0_rom.bin",Path(fwdir)/flash,Path(sd)]:
    if not p.is_file() or p.is_symlink(): raise SystemExit("missing/symlinked input: "+str(p))
if Path(flash).name!=flash: raise SystemExit("firmware must be a basename")
if (Path(fwdir)/flash).stat().st_size!=16*1024*1024: raise SystemExit("wrong Flash geometry")
if Path(sd).stat().st_size%512: raise SystemExit("wrong SD sector alignment")
if not Path(work).is_absolute() or Path(work).is_symlink(): raise SystemExit("unsafe work directory")
with Path(binary).open("rb") as stream:
    if hashlib.file_digest(stream,"sha256").hexdigest()!=sha: raise SystemExit("QEMU binary SHA mismatch")
print("Pinned patched QEMU / ROM / source media verified")
PY
if [[ "$MODE" == "--check" ]]; then exit 0; fi
if docker container inspect "$NAME" >/dev/null 2>&1; then
  echo "Existing container is never replaced: $NAME" >&2; exit 2
fi
if command -v lsof >/dev/null && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t | grep -q .; then
  echo "Port in use: $PORT" >&2; exit 2
fi
docker image inspect "$IMAGE" >/dev/null
if [[ "$MODE" == "--reuse" ]]; then
  [[ -d "$WORK" && -f "$WORK/flash.bin" && -f "$WORK/sd.img" &&
     ! -L "$WORK/flash.bin" && ! -L "$WORK/sd.img" ]] || {
    echo "No complete persistent image to reuse" >&2; exit 2; }
else
  [[ ! -e "$WORK" ]] || { echo "Work already exists (use --reuse)" >&2; exit 2; }
  mkdir -m 700 "$WORK"
fi
docker run -d --rm --name "$NAME" --platform linux/arm64 --network bridge \
  -p "127.0.0.1:$PORT:$PORT" \
  -v "$BIN:/qemu.bin:ro" -v "$ROM:/roms:ro" -v "$FW:/fw:ro" \
  -v "$SD:/source-sd.img:ro" -v "$WORK:/persist" -v "$TOOLS:/scripts:ro" \
  -w /tmp "$IMAGE" \
  python3 -u /scripts/serve_einkqemu11_browser.py \
  --qemu /qemu.bin --rom-dir /roms --flash "/fw/$FLASH" \
  --sd /source-sd.img --firmware-dir /fw --work-dir /persist \
  --wifi --enable-power --host 0.0.0.0 --container-bind --port "$PORT"
echo "Patched QEMU11 browser: http://127.0.0.1:$PORT/"
echo "Persistent guest media: $WORK"
echo "Stop only this instance: docker stop $NAME"
