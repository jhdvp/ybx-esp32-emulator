#!/bin/sh
# Rebuild the validated 0101 virtual POWER QEMU offline from pinned sources.
# This script NEVER changes a running QEMU/browser or its source checkout.
# Explicit --build is required because a fresh build takes minutes/space.
set -eu

HERE=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
UPSTREAM=${YBX_QEMU_UPSTREAM_ROOT:-"$HOME/.local/state/ybx-emulator-upstream/qemu"}
IMAGE=${YBX_QEMU_BUILDER_IMAGE:-ybx-qemu-builder:20261029}
DOCKER_BIN=${YBX_DOCKER_BIN:-docker}
EXPECTED_BINARY_SHA=7b729fba1ceefdc67a55dc9ddd6d359699e16174a52d741ea243849c35e2accc
WORK_DIR=
BUILD=0
JOBS=3

usage() {
  cat >&2 <<'EOF'
Usage: build_x4pro_power_clean_room.sh --work-dir EMPTY_DIRECTORY --build
       [--qemu-src UPSTREAM_GIT_DIRECTORY] [--jobs 1..N] [--image TAG]

Recomposes pinned 26-patch QEMU + research 0096..0101, copies only three
locally pinned external subproject Git trees, and builds in offline Docker.
No network access. No deployment or update of browser 8784/8794.
The --work-dir must exist and must not already contain a compose subdir.
The generated output is deliberately retained for inspection; remove it
manually after recording checksums and completing acceptance tests.
EOF
}

die() { echo "clean-room: $*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --build) BUILD=1 ;;
    --work-dir) [ "$#" -ge 2 ] || die "missing --work-dir value"; WORK_DIR=$2; shift ;;
    --qemu-src) [ "$#" -ge 2 ] || die "missing --qemu-src value"; UPSTREAM=$2; shift ;;
    --jobs) [ "$#" -ge 2 ] || die "missing --jobs value"; JOBS=$2; shift ;;
    --image) [ "$#" -ge 2 ] || die "missing --image value"; IMAGE=$2; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage; die "unexpected argument $1" ;;
  esac
  shift
done

[ "$BUILD" -eq 1 ] || die "use --build to authorize an offline compile (or --help)"
[ -n "$WORK_DIR" ] || die "specify a new --work-dir"
[ -d "$WORK_DIR" ] || die "work directory does not exist: $WORK_DIR"
[ -d "$UPSTREAM/.git" ] || die "upstream Git object repository missing: $UPSTREAM"
[ ! -e "$WORK_DIR/compose" ] || die "refusing to reuse or overwrite $WORK_DIR/compose"
printf '%s\n' "$JOBS" | grep -Eq '^[1-9][0-9]*$' || die "jobs must be a positive integer"
command -v python3 >/dev/null || die "python3 missing"
command -v git >/dev/null || die "git missing"
command -v "$DOCKER_BIN" >/dev/null || die "$DOCKER_BIN missing"

# Fully verify each offline third-party dependency before using it. The
# versions are determined by the upstream QEMU subprojects/*.wrap files.
verify_git_dep() {
  dep=$1 expected=$2
  src="$UPSTREAM/subprojects/$dep"
  [ -d "$src/.git" ] || die "pinned offline dependency not checked out: $src"
  actual=$(git -C "$src" rev-parse HEAD) || die "cannot read $src commit"
  [ "$actual" = "$expected" ] || die "$dep commit drift: $actual != $expected"
  wrap="$UPSTREAM/subprojects/$dep.wrap"
  [ -f "$wrap" ] || die "missing $wrap"
  grep -Fq "revision = $expected" "$wrap" || die "$dep wrap revision drift"
  echo "verified pinned dependency $dep $actual"
}

verify_git_dep keycodemapdb f5772a62ec52591ff6870b7e8ef32482371f22c6
verify_git_dep berkeley-softfloat-3 b64af41c3276f97f0e181920400ee056b9c88037
verify_git_dep berkeley-testfloat-3 e7af9751d9f9fd3b47911f51a5cfd08af256a9ab

OUT="$WORK_DIR/compose"
TREE="$OUT/qemu-compose"
python3 "$HERE/check_qemu_patch_composition.py" compose \
  --chain full --qemu-src "$UPSTREAM" --out "$OUT"

for file in \
  0096-s3-rtc-sleep-registers-explicit-wakeup.patch \
  0097-s3-rtc-explicit-deep-wakeup-dig-reset.patch \
  0098-gt911-ignore-short-reset-pulse.patch \
  0098-i2c-gt911-readonly-qom-telemetry.patch \
  0100-rtc-ext1-gpio-level-wakeup.patch \
  0101-x4pro-power-button-gpio3-rtc3-reset-retention.patch
do
  patch="$HERE/experiments/$file"
  [ -f "$patch" ] || die "missing patch $patch"
  git -C "$TREE" apply --check "$patch"
  git -C "$TREE" apply "$patch"
  echo "applied $file"
done

# These subprojects cannot be fetched by the offline container. Copy from the
# pinned upstream Git subprojects, NOT from arbitrary mutable build outputs.
for dep in keycodemapdb berkeley-softfloat-3 berkeley-testfloat-3
do
  src="$UPSTREAM/subprojects/$dep"
  [ ! -e "$TREE/subprojects/$dep" ] || die "unverified existing subproject $dep"
  if ! cp -c -R "$src" "$TREE/subprojects/$dep" 2>/dev/null; then
    [ ! -e "$TREE/subprojects/$dep" ] || rm -rf -- "$TREE/subprojects/$dep"
    cp -R "$src" "$TREE/subprojects/$dep"
  fi
  [ "$(git -C "$TREE/subprojects/$dep" rev-parse HEAD)" = "$(git -C "$src" rev-parse HEAD)" ] \
    || die "copied subproject $dep mismatches pinned SHA"
done

# Keep configure flags aligned with QEMU_BRINGUP.md and the audited build.
# --network none also prevents Meson/Python from fetching unexpected packages.
echo "starting offline QEMU build in $TREE (jobs=$JOBS)"
"$DOCKER_BIN" run --rm --network none --platform linux/arm64 \
  -v "$TREE:/src" -w /src "$IMAGE" sh -lc \
  "set -eu; mkdir -p build-ybx; cd build-ybx; \
   ../configure --target-list=xtensa-softmmu,riscv32-softmmu \
     --enable-gcrypt --disable-nettle --disable-docs --disable-gtk \
     --disable-sdl --disable-vnc --disable-curses --disable-tools --disable-werror; \
   ninja -j$JOBS qemu-system-xtensa"

BIN="$TREE/build-ybx/qemu-system-xtensa"
[ -f "$BIN" ] || die "compiler succeeded but binary missing"
SHA=$(shasum -a 256 "$BIN" | awk '{print $1}')
printf 'clean-room binary: %s\nSHA-256: %s\n' "$BIN" "$SHA"
[ "$SHA" = "$EXPECTED_BINARY_SHA" ] \
  || die "binary is NOT identical to accepted 0101 ($EXPECTED_BINARY_SHA)"
echo 'CLEAN_ROOM_BYTE_IDENTICAL_PASS'
