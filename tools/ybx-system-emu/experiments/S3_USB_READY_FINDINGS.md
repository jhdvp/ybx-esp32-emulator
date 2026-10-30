## S3 USB Serial/JTAG ROM boot correction (29 October 2026)

Disassembled ROM usb_uart_device_tx_one_char at 0x4004903c..0x4004907e: it reads USB Serial/JTAG EP1 CONF at 0x60038004 and tests bit 1, SERIAL_IN_EP_DATA_FREE. In upstream hw/misc/esp32c3_jtag.c, also used by the S3 virtual machine, reads returned zero for every register. The C6 model already correctly returns TX FIFO ready at this offset.

The zero return causes the S3 ROM to time out and access an uninitialised ets_ops_table_ptr delay callback, leading to debug exception at 0x40000280/0x40000288. The accepted patch patches/0003-usb-serial-jtag-tx-ready.patch implements the TX-ready register response, not an instruction bypass or altered YBX binary.

Proof: Xtensa QEMU target rebuilt. The real X4 Pro factory image advances beyond previous ROM exception markers (0x40049057, 0x40000280 and 0x40000288 absent in fresh executed-instruction trace). The ROM subsequently reaches ets_fatal_exception_handler at 0x40050f3f; application boot UART is still not observed, so the full boot remains BLOCKED. CPU 1 still waits for the startup handoff. Evidence at ~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/qemu-boot-diagnostics/s3-usbready-exec.log.

Apply together with GDMA patch via apply_qemu_patches.sh. Rejected S3 ROM DRAM experiment under experiments/ is not applied. Remaining: isolate later ROM fatal handler, then verify bootloader, guest SDMMC, SPI panel, Wi-Fi and TLS.
