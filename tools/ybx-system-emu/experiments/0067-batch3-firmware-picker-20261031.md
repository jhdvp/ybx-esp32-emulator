# 0067 — firmware picker: boot the operator's own build, honestly

Date: 2026-10-31. Scope: the standalone YBX ESP32 emulator browser bridge.

The owner asked to load his own firmware (a CrossPoint build, inkMOD), not only
the YBX factory image. The chosen design matches the ROM picker: a folder of
candidate images on the Mac, read-only for the server, plus a picker in the
page.

## What was added

- `--firmware-dir` (default: the directory of `--flash`), mounted read-only in
  the container at `/fw` by `start_x4pro_browser.sh`; `YBX_FIRMWARE_DIR`
  overrides it. The launcher seeds the factory flash into the folder once so
  "switch back to factory" is always possible.
- `GET /api/firmware` lists the candidates with name, size, SHA-256, image
  kind and provenance. Only `.bin`/`.img` files are offered.
- `POST /api/firmware` with `{"name": "..."}` validates the name (plain file
  name, no separators, no `..`, must resolve inside the folder, no symlink),
  validates the image itself, installs it as the guest flash (and the paired
  `<stem>.img` SD image when there is one), restarts the guest and answers with
  the accepted name, SHA-256 and provenance.
- Image kind is decided by the bytes, not by the name: `flash` = ESP magic
  0xE9 at 0x0, partition table 0xAA50 at 0x8000, application magic 0xE9 at
  0x10000; `sd` = MBR signature 0x55AA at 0x1FE; anything else is `unknown` and
  is refused before the guest is touched.
- **Honesty fix:** `/api/state` used to hardcode `factory_guest: true`. It is
  now derived from the active flash SHA-256: true only for the known factory
  image `206ed966…`. A custom build reports `factory_guest: false` with a
  `custom` provenance marker, and the page says so.
- The page carries a second picker next to the ROM picker, with the active
  image, its SHA-256, its provenance, a disabled-while-busy button and a
  visible error line; the page stays usable when the list cannot be read.

## Live verification (running instance, real factory media)

| Check | Result |
|---|---|
| `GET /api/firmware` | 7 entries: the active factory image (shown even though it lives outside the folder), two own builds, three upstream builds, one application-only image with kind `unknown` |
| `POST /api/firmware` with an application-only image | 400 "not an ESP flash image (kind=unknown); refusing to load it" |
| `POST /api/firmware` with `../qa-20261029/…` | 400 "Firmware name must be a plain file name inside --firmware-dir" |
| switch to `ybx-ui-c663d6a0-on-factory-base.bin` (own build) | accepted; guest booted, frames 0 → 1 → 4, Home SHA `e79c2ad5…`, `factory_guest: false` |
| switch to `ybx-settings-092c2be5-on-factory-base.bin` (own build) | accepted; guest booted, frames 4, Home SHA `e79c2ad5…` |
| switch to `crosspoint-2.0.0rc-x4pro-flash-16m.bin` (upstream build) | accepted; the guest died immediately - see the limits below |
| switch back to `s3-flash-complete-rom-test.bin` | accepted; guest booted, Home SHA `e79c2ad5…`, `factory_guest: true` |

## Measured limits of the upstream images

The three upstream images in the folder (stock CrossPoint 2.0.0rc, stock
CrossPoint 1.6.5, inkMOD v1.1.10) were merged the way the local release
packaging does it: X4 Pro bootloader at 0x0, partition table at 0x8000,
application at 0x10000, padded to 16 MiB.

- With that **CrossPoint bootloader**, the guest crashes QEMU with SIGSEGV.
  The crash follows the bootloader, not the application: a control image with
  the CrossPoint bootloader and the *factory* application crashes identically.
- With the **factory bootloader and partition table** plus a foreign
  application, the guest boots the ROM and then stops before the application
  starts - no frames, no application output.
- The own YBX builds (c663d6a0, 092c2be5) placed on the factory base boot and
  paint normally.

So the emulator boots the factory image and the operator's own YBX builds;
upstream CrossPoint and inkMOD images do not boot yet. The open question is
what the CrossPoint-built bootloader expects from the flash that the emulated
ESP32-S3 does not provide (flash size/layout or bootloader configuration), not
the applications themselves.

## Files

- `tools/ybx-system-emu/serve_x4pro_browser.py` - the picker, the validation,
  the derived `factory_guest`, the page.
- `tools/ybx-system-emu/start_x4pro_browser.sh` - `YBX_FIRMWARE_DIR`, the
  read-only mount, the factory seed.
- `tools/ybx-system-emu/test_serve_x4pro_browser.py` - listing, kinds, refusal
  of a non-flash file, refusal of traversal, the switch (installs the image,
  resets the frame cache, restarts the guest, copies the paired SD image) and
  the derived `factory_guest`.
