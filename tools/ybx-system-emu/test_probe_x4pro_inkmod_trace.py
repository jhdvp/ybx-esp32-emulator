"""Host-only tests for the inkMOD trace probe decoders."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from probe_x4pro_inkmod_trace import (
    RTC_BASE,
    changed_blocks,
    classify_outcome,
    classify_pc,
    parse_registers,
    parse_xp_words,
    summarize_exec_log,
    summarize_uart,
    visible_frame_sha,
)

REGISTER_DUMP = (
    "\r\nCPU#1\r\nPC=4037cb2c\r\n\r\n"
    "      CCOUNT=0b4b9d46         PRID=0000abab       ICOUNT=00000000\r\n"
    "        EPC1=4004e8ab     EXCCAUSE=00000004        DEPC=00000000\r\n"
    "          PS=00060623        MISC0=00000000    INTENABLE=1300000f\r\n"
    "\r\n"
    " A00=80377420  A01=3fcedae0  A02=00010000  A03=00000000\r\n"
    " A08=6000804c  A10=60008044\r\n"
    "\r\n"
    "AR00=80377420 AR01=3fcedae0 AR02=00010000 AR03=00000000\r\n"
)


def timeline(pairs, step=0.25):
    return [{"t": round((index + 1) * step, 3),
             "cpu0_pc": cpu0, "cpu1_pc": cpu1}
            for index, (cpu0, cpu1) in enumerate(pairs)]


class RegisterDecoding(unittest.TestCase):
    def test_parses_pc_ps_exccause_and_window_registers(self):
        registers = parse_registers(REGISTER_DUMP)
        self.assertEqual(registers["PC"], 0x4037CB2C)
        self.assertEqual(registers["PS"], 0x00060623)
        self.assertEqual(registers["EXCCAUSE"], 4)
        self.assertEqual(registers["A00"], 0x80377420)
        self.assertEqual(registers["A02"], 0x00010000)
        self.assertEqual(registers["A10"], 0x60008044)
        self.assertEqual(registers["AR02"], 0x00010000)
        self.assertNotIn("EPC1", registers)

    def test_rejects_dump_without_pc(self):
        self.assertNotIn("PC", parse_registers("CPU#0\r\nA00=00000001\r\n"))


class XpDecoding(unittest.TestCase):
    def test_parses_words_across_lines_and_ignores_noise(self):
        text = ("Linking TBs 0xffff62c00140 index 0 -> 0xffff62c00240\r\n"
                "000000003fc99e60: 0x00000002 0x00000000 0x00000002 "
                "0x00000000\r\n"
                "000000003fc99e70: 0x00000008 0x0000015e\r\n")
        self.assertEqual(parse_xp_words(text),
                         [2, 0, 2, 0, 8, 0x15E])

    def test_tolerates_trailing_text(self):
        text = "0000000060008044: 0x00000000 \r\n"
        self.assertEqual(parse_xp_words(text), [0])


class FrameHashing(unittest.TestCase):
    def test_sha_covers_decoded_bytes(self):
        payload = bytes(range(16))
        digest, length = visible_frame_sha(payload.hex())
        self.assertEqual(length, 16)
        self.assertEqual(digest, hashlib.sha256(payload).hexdigest())

    def test_tolerates_whitespace_and_rejects_odd_hex(self):
        self.assertEqual(visible_frame_sha(" ffff\n")[1], 2)
        with self.assertRaises(ValueError):
            visible_frame_sha("abc")


class FlashComparison(unittest.TestCase):
    def test_reports_matching_and_changed_blocks(self):
        with tempfile.TemporaryDirectory() as work:
            base = Path(work)
            first = base / "a.bin"
            second = base / "b.bin"
            first.write_bytes(b"\x00" * 8192)
            second.write_bytes(b"\x00" * 8192)
            self.assertEqual(changed_blocks(first, second), [])
            second.write_bytes(b"\x00" * 4096 + b"\x01" + b"\x00" * 4095)
            self.assertEqual(changed_blocks(first, second), [4096])

    def test_size_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as work:
            base = Path(work)
            (base / "a.bin").write_bytes(b"\x00" * 4096)
            (base / "b.bin").write_bytes(b"\x00" * 8192)
            with self.assertRaises(ValueError):
                changed_blocks(base / "a.bin", base / "b.bin")


class PcClassification(unittest.TestCase):
    def test_known_wait_ranges_are_half_open(self):
        self.assertEqual(classify_pc(0x4037CB2C), "rtc_sleep_start")
        self.assertEqual(classify_pc(0x4037CAB8), "rtc_sleep_start")
        self.assertEqual(classify_pc(0x4037A4A8), "ipc_isr_wait")
        self.assertIsNone(classify_pc(0x4037CB60))
        self.assertIsNone(classify_pc(0x4004798C))
        self.assertIsNone(classify_pc(None))


class OutcomeClassification(unittest.TestCase):
    def test_wedged_cores_with_zero_int_raw_are_a_sleep_stall(self):
        report = classify_outcome(
            timeline([(0x4037A4A8, 0x4037CB2C)] * 12),
            {"frames": 0, "commands": 0},
            {RTC_BASE + 0x44: 0},
            {"ipc_finish_cmd": 0},
        )
        self.assertEqual(report["verdict"], "deep_sleep_poll_stalled")
        self.assertEqual(report["cpu0_tail"], "ipc_isr_wait")
        self.assertEqual(report["cpu1_tail"], "rtc_sleep_start")
        self.assertEqual(report["wedge_since_s"], 0.25)
        self.assertEqual(report["ipc_finish_cmd"], 0)

    def test_wedge_since_tracks_the_final_contiguous_run(self):
        boots = [(0x4004798C, 0x40043A3E)] * 10
        wedge = [(0x4037A4A8, 0x4037CB2C)] * 10
        report = classify_outcome(
            timeline(boots + wedge),
            {"frames": 0, "commands": 0},
            {RTC_BASE + 0x44: 0},
            {"ipc_finish_cmd": 0},
        )
        self.assertEqual(report["verdict"], "deep_sleep_poll_stalled")
        self.assertEqual(report["wedge_since_s"], 2.75)

    def test_a_painted_frame_outranks_a_later_wedge(self):
        report = classify_outcome(
            timeline([(0x4037A4A8, 0x4037CB2C)] * 12),
            {"frames": 2, "commands": 82},
            {RTC_BASE + 0x44: 0},
            {},
        )
        self.assertEqual(report["verdict"], "painted")

    def test_nonzero_int_raw_is_not_reported_as_a_stall(self):
        report = classify_outcome(
            timeline([(0x4037A4A8, 0x4037CB2C)] * 12),
            {"frames": 0},
            {RTC_BASE + 0x44: 1},
            {},
        )
        self.assertEqual(report["verdict"], "deep_sleep_poll_active")

    def test_single_core_signatures_are_named_separately(self):
        rtc_only = classify_outcome(
            timeline([(0x4004798C, 0x4037CB2C)] * 12),
            {"frames": 0}, {RTC_BASE + 0x44: 0}, {})
        self.assertEqual(rtc_only["verdict"], "rtc_sleep_poll_only")
        ipc_only = classify_outcome(
            timeline([(0x4037A4A8, 0x40043A3E)] * 12),
            {"frames": 0}, {RTC_BASE + 0x44: 0}, {})
        self.assertEqual(ipc_only["verdict"], "ipc_isr_stall_only")

    def test_epd_traffic_without_frame_is_reported(self):
        report = classify_outcome(
            timeline([(0x4004798C, 0x40043A3E)] * 12),
            {"frames": 0, "commands": 4, "ram-writes": 10},
            {RTC_BASE + 0x44: 0}, {})
        self.assertEqual(report["verdict"], "epd_active_no_frame")

    def test_unrecognised_pcs_stay_unknown(self):
        report = classify_outcome(
            timeline([(0x4004798C, 0x40043A3E)] * 12),
            {"frames": 0}, {RTC_BASE + 0x44: 0}, {})
        self.assertEqual(report["verdict"], "running_or_unknown")

    def test_empty_timeline_is_not_a_verdict(self):
        report = classify_outcome([], {}, {}, {})
        self.assertEqual(report["verdict"], "no_samples")

    def test_rom_panic_outranks_later_counters(self):
        report = classify_outcome(
            timeline([(0x400494FF, 0x40043A3E)] * 12),
            {"frames": 0}, {RTC_BASE + 0x44: 0}, {},
            {"panic": True, "panic_text": "Guru Meditation Error",
             "app_markers": []})
        self.assertEqual(report["verdict"], "rom_panic")
        self.assertEqual(report["reason"], "Guru Meditation Error")

    def test_panic_outside_rom_is_a_guest_panic(self):
        report = classify_outcome(
            timeline([(0x42001234, 0x4037CB2C)] * 12),
            {"frames": 0}, {RTC_BASE + 0x44: 0}, {},
            {"panic": True, "panic_text": "Guru Meditation Error",
             "app_markers": ["Arduino"]})
        self.assertEqual(report["verdict"], "guest_panic")


class UartSummary(unittest.TestCase):
    def test_rom_only_uart_is_boot_evidence_without_app_or_panic(self):
        raw = (b"ESP-ROM:esp32s3-20210327\r\n"
               b"rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)\r\n"
               b"entry 0x40377798\r\n")
        summary = summarize_uart(raw)
        self.assertTrue(summary["rom_seen"])
        self.assertTrue(summary["bootloader_seen"])
        self.assertEqual(summary["app_markers"], [])
        self.assertFalse(summary["panic"])
        self.assertEqual(summary["bytes"], len(raw))

    def test_panic_string_is_detected(self):
        raw = b"Guru Meditation Error: Core 0 panic'ed (LoadProhibited)\r\n"
        summary = summarize_uart(raw)
        self.assertTrue(summary["panic"])
        self.assertEqual(summary["panic_text"],
                         "Guru Meditation Error: Core 0 panic'ed (LoadProhibited)")

    def test_app_markers_are_reported(self):
        summary = summarize_uart(b"inkMOD version: 1.1.10\r\n")
        self.assertEqual(summary["app_markers"], ["inkMOD version"])


class ExecLogSummary(unittest.TestCase):
    def test_counts_filtered_trace_lines_only(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "exec.log"
            path.write_text(
                "Linking TBs 0xffff62c00140 index 0 -> 0xffff62c00240\n"
                "Trace 1: 0xffff66bfbc40 "
                "[00000000/000000004037742e/00158010/ff008000]\n"
                "Trace 2: 0xffff66bfbc40 "
                "[00000000/0000000040377448/00158010/ff008000]\n"
                "Trace 3: 0xffff66bfbc40 "
                "[00000000/000000004037cb2c/00158010/ff008000]\n"
                "Trace 4: 0xffff66bfbc40 "
                "[00000000/000000004037a4a8/00158010/ff008000]\n")
            summary = summarize_exec_log(path)
        self.assertTrue(summary["exists"])
        self.assertEqual(summary["hits"]["deep_branch_body"], 1)
        self.assertEqual(summary["hits"]["light_branch_body"], 1)
        self.assertEqual(summary["hits"]["rtc_int_raw_poll"], 1)
        self.assertEqual(summary["hits"]["ipc_isr_wait"], 1)
        self.assertEqual(summary["hits"]["deep_sleep_wrapper"], 0)

    def test_missing_log_is_explicit(self):
        summary = summarize_exec_log(Path("/nonexistent/exec.log"))
        self.assertFalse(summary["exists"])
        self.assertEqual(set(summary["hits"].values()), {0})


if __name__ == "__main__":
    unittest.main()
