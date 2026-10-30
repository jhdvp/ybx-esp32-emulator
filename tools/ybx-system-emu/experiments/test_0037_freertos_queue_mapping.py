"""Host-side validation for 0037-freertos-queue-tcb-probe.gdb.

Checks that the DWARF offsets, symbol addresses and source-line mappings baked
into the probe still hold for the matching X4 Pro 092c2be5 factory ELF.
Read-only; skips when the toolchain or firmware ELF is absent.
"""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ELF_DEFAULT = Path(
    "/Users/yaraslauhancharou/.local/state/xteink-agent-workspaces/"
    "fw-092c2be5-symbols-20261029/.pio/build/x4pro/firmware.elf"
)
ELF = Path(os.environ.get("X4PRO_FIRMWARE_ELF", ELF_DEFAULT))
GDB = shutil.which("xtensa-esp32s3-elf-gdb") or Path(
    "/Users/yaraslauhancharou/.platformio/packages/tool-xtensa-esp-elf-gdb/"
    "bin/xtensa-esp32s3-elf-gdb"
)
READELF = Path(
    "/Users/yaraslauhancharou/.platformio/packages/toolchain-xtensa-esp-elf/"
    "bin/xtensa-esp32s3-elf-readelf"
)

# Machine addresses recorded by 0036/0037b/0041/0042 for the 092c2be5 build.
EPD_SEM = 0x3FCEC294
EPD_SEM_RECV_LIST = 0x3FCEC2B8
RENDER_TCB = 0x3FCEC420
X_DELAYED_1 = 0x3FCAC00C
S_EPD_SEM_GLOBAL = 0x3FCA1DA8


def have_tools():
    return ELF.is_file() and Path(GDB).is_file() and READELF.is_file()


@unittest.skipUnless(have_tools(), "factory ELF or Espressif binutils missing")
class FreertosProbeLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(
            [str(READELF), "-sW", str(ELF)], capture_output=True, text=True,
            check=True).stdout
        cls.symbols = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 8 and parts[3] in ("FUNC", "OBJECT"):
                try:
                    cls.symbols.setdefault(parts[-1], int(parts[1], 16))
                except ValueError:
                    pass

    def gdb(self, *commands):
        argv = [str(GDB), "-batch", "-q", str(ELF)]
        for c in commands:
            argv += ["-ex", c]
        return subprocess.run(argv, capture_output=True, text=True,
                              check=True).stdout

    def test_recorded_kernel_statics(self):
        for name, addr in (
            ("xDelayedTaskList1", X_DELAYED_1),
            ("_ZN7freeinkL16s_epdRefreshDoneE", S_EPD_SEM_GLOBAL),
            ("xQueueSemaphoreTake", 0x4038473C),
            ("xTaskGenericNotify", 0x40387444),
            ("vTaskPlaceOnEventList", 0x40386498),
            ("xTickCount", 0x3FCABF94),
        ):
            self.assertEqual(self.symbols.get(name), addr, name)

    def test_queue_and_tcb_offsets_match_recording(self):
        out = self.gdb(
            "print &((struct QueueDefinition*)0)->xTasksWaitingToReceive",
            "print &((struct tskTaskControlBlock*)0)->xEventListItem.pvContainer",
            "print &((struct tskTaskControlBlock*)0)->xStateListItem.xItemValue",
            "print &((struct tskTaskControlBlock*)0)->ulNotifiedValue",
            "print &((struct tskTaskControlBlock*)0)->ucNotifyState",
            "print &((XtExcFrame*)0)->pc",
            "print &((XtExcFrame*)0)->a0",
        )
        values = [int(tok, 16) for line in out.splitlines() if "$" in line
                  for tok in line.split() if tok.startswith("0x")]
        self.assertEqual(values[:7], [0x24, 0x28, 0x04, 0x158, 0x15C, 0x04, 0x0C])
        # The recorded waiter pvContainer is exactly this list header.
        self.assertEqual(EPD_SEM + values[0], EPD_SEM_RECV_LIST)
        # The recorded "stays on xDelayedTaskList1" address is that header.
        self.assertEqual(self.symbols["xDelayedTaskList1"], X_DELAYED_1)

    def test_dwarf_lines_match_cited_sources(self):
        import re
        out = self.gdb("info line *0x40387444", "info line *0x4201ea6e",
                       "info line *0x4201ea15", "info line *0x4038473c")
        self.assertRegex(out, r"Line 5903 of .*tasks\.c")
        self.assertRegex(out, r"Line 460 of .*EpdBus\.cpp")
        self.assertRegex(out, r"Line 445 of .*EpdBus\.cpp")
        self.assertRegex(out, r"Line 1699 of .*queue\.c")

    def test_wait_sites_inside_wait_refresh_complete(self):
        base = self.symbols["_ZN7freeink6EpdBus19waitRefreshCompleteEPKc"]
        size = 355
        for site in (0x4201E9E5, 0x4201EA15, 0x4201EA6E):
            self.assertTrue(base <= site < base + size, hex(site))


if __name__ == "__main__":
    unittest.main()
