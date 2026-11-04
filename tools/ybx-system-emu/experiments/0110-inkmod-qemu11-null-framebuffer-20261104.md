# 0110 — inkMOD 1.1.10 / QEMU11: early null framebuffer

Date: 2026-11-04. **The migration blocker remains open. Production 8784 is unchanged.**

## Exact source and image provenance

- Tag: alpzoloto-sudo/inkmod v1.1.10; source commit eaf0ce32435339e4fd28d8a8d33638bf998be713.
- Release toolchain: pioarduino 55.03.37; ESP32-S3 N16R8, dio_opi, NDEBUG, LOG_LEVEL=-1, single framebuffer.
- Original unmodified 16-MiB flash SHA-256: 3fb63a3101fd662a3914c836a2c5ef4a571bb89a54eb1af4b1834326007258f0.
- Locally **rebuilt** 16-MiB flash SHA-256: caf46154c7bf4363bf67cef7c2cda2788f906dc15434820939cc83864a067f95.
- Matching rebuilt debug ELF SHA-256: 98ffc195c84b11354201b205a93c8112f7956efde1b73e0b6bc3224ca8136a85 (77 MiB, DWARF symbols).
- **The original and rebuilt images differ.** The original PC 0x4204541c MUST NOT be mapped directly with this rebuilt ELF.

## Real guest reproduction

Ran the rebuilt release image, on a private copy of flash/SD, with official serialx/eink-qemu 0.9.2 / QEMU 11.1.2, machine x4pro, NIC disabled. The matching panic UART reports:

    Guru Meditation Error: Core 1 panic'ed (LoadProhibited)
    PC       : 0x420453a8
    A2       : 0x3fca1660
    A10      : 0x000060f0
    EXCVADDR : 0x000060f0
    ELF file SHA256: 98ffc195c

The ELF hash prefix in the UART **matches** the rebuilt ELF. Only this rebuilt crash is symbolicated below; the subsequent Unhandled debug exception is a secondary panic.

Symbolication:
- 0x420453a8: GfxRenderer::drawPixel(int,int,bool) in lib/GfxRenderer/GfxRenderer.cpp:700.
- Relevant callers: drawLine -> fillRect -> fillRectDither -> fillRoundedRect.
- 0x4204538f: l32i.n a10, a2, 20 — load framebuffer member.
- 0x4204539c: add.n a10, a10, a9 — add pixel byte offset.
- 0x420453a8: l8ui a11, a10, 0 — faulting byte read at 0x000060f0.

In source, GfxRenderer::frameBuffer is initialized to nullptr and later assigned display.getFrameBuffer() by GfxRenderer::begin(). FreeInkDisplay::begin() sets its own framebuffer to the result of allocFrameBufferStorage(). The X4 Pro release environment does **not** enable FREEINK_FB_PSRAM, therefore the framebuffer allocator uses malloc(bufferSize). A failed allocation is one plausible explanation, **not yet demonstrated**. Missing/incorrect renderer setup is another.

Important: GfxRenderer::begin() contains assert(false) for a missing framebuffer, but release builds define NDEBUG, so that assertion is compiled out. The guest can continue and fault at drawPixel() instead.

## Safety-checked analysis command

    python3 tools/ybx-system-emu/symbolicate_inkmod_panic.py \
      --uart /private/inkmod-rebuilt-uart.log \
      --elf /private/matching/firmware.elf \
      --addr2line /path/to/xtensa-esp32s3-elf-addr2line

The decoder matches the ELF SHA-256 prefix from the **actual guest panic** before running addr2line. A same-version but different ELF is refused (exit 2); missing marker is also refused. No original firmware or SD image is uploaded to GitHub.

A first live GDB run with QEMU -S -gdb closed the remote connection during continue, before an allocator/begin breakpoint was hit. It produced no trustworthy malloc trace.

## Next evidence gates

1. Record FreeInkDisplay::begin() and GfxRenderer::begin() entry, framebuffer pointer and size, with a verified GDB/QMP bridge or instrumented developer **diagnostic** build.
2. Measure the internal and SPIRAM heap free/largest block at the allocation call; determine if the allocation actually returned null, or if initialization was skipped.
3. Reproduce the same rebuilt image on the old working QEMU9 and compare initialization without changing the stable 8784 service.
4. Fix the QEMU11-side discrepancy if confirmed; do not merely dereference-protect/patch the guest firmware to claim emulator parity.
5. Only then rerun **original** inkMOD and require no panic, a native Home frame, genuine GT911 navigation, GPIO3 Power -> RTC EXT1, repeated Flash/SD mount integrity, plus the already passing CrossPoint native Wi-Fi/HTTP gates.
6. Preserve separate old QEMU9 rollback snapshots and stable 8784 until all acceptance gates are green.
