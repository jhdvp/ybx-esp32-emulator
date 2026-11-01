"""Host-only tests for the inkMOD stop root-cause analyzer.

Two layers: pure logic against synthetic images and synthetic QEMU trees, plus
a small set of assertions against the artifacts that actually sit on this disk
(skipped when they are absent). No QEMU, no container, no guest is started.
"""
import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import inkmod_forced_wake as ifw
import inkmod_root_cause as irc


def build_image(segments, offset=irc.APP_OFFSET, magic=irc.IMAGE_MAGIC,
                tail=0):
    """Serialise an ESP-app-style image at the slot offset the tool reads.

    ``segments`` is [(vaddr, bytes), ...]; the layout mirrors a real OTA slot:
    padding, 24-byte extended header, then per segment an 8-byte header
    immediately followed by its body. ``tail`` appends zero bytes so the
    checksum position falls inside the image, as it does in a flashed slot.
    """
    data = bytearray(offset)
    head = bytearray(irc.HEADER_BYTES)
    head[0] = magic
    head[1] = len(segments)
    head[4:8] = (0x403760D8).to_bytes(4, "little")
    head[0xC:0xE] = (0x0009).to_bytes(2, "little")
    body = bytearray(head)
    for vaddr, blob in segments:
        body += vaddr.to_bytes(4, "little") + len(blob).to_bytes(4, "little")
        body += blob
    return bytes(data) + bytes(body) + bytes(tail)


def word(value):
    return value.to_bytes(4, "little")


def pad_to(blob, size, anchor=0):
    """Place blob at ``anchor`` inside a ``size``-byte zero region."""
    out = bytearray(size)
    out[anchor:anchor + len(blob)] = blob
    return bytes(out)


class ImageWalk(unittest.TestCase):
    def test_parses_header_and_segment_file_offsets(self):
        iram = pad_to(word(irc.RTC_BASE + 0x44), 0x4000, anchor=0x100)
        data = build_image([(0x40374000, iram), (0x3FC98D00, bytes(0x200))])
        image = irc.esp_app_segments(data)
        self.assertEqual(image["segment_count"], 2)
        self.assertEqual(image["entry_addr"], 0x403760D8)
        self.assertEqual(image["chip_id"], 0x0009)
        first, second = image["segments"]
        self.assertEqual(first["file_offset"],
                         irc.APP_OFFSET + irc.HEADER_BYTES
                         + irc.SEGMENT_HEADER_BYTES)
        self.assertEqual(second["file_offset"],
                         first["file_offset"] + 0x4000 + irc.SEGMENT_HEADER_BYTES)
        self.assertEqual(first["region"], "internal SRAM instruction (iram0)")
        self.assertEqual(second["region"], "internal SRAM data (dram0)")

    def test_rejects_bad_magic_and_short_images(self):
        with self.assertRaises(ValueError):
            irc.esp_app_segments(build_image([(0x40374000, bytes(8))],
                                              magic=0x00))
        with self.assertRaises(ValueError):
            irc.esp_app_segments(bytes(30))

    def test_rejects_segment_running_past_image_end(self):
        data = build_image([(0x40374000, bytes(64))])
        truncated = data[:-32]
        with self.assertRaises(ValueError):
            irc.esp_app_segments(truncated)

    def test_vaddr_mapping_is_half_open(self):
        data = build_image([(0x40374000, bytes(0x100))])
        image = irc.esp_app_segments(data)
        self.assertIsNotNone(irc.vaddr_to_file_offset(image, 0x40374000))
        self.assertIsNone(irc.vaddr_to_file_offset(image, 0x40374100))
        self.assertIsNone(irc.vaddr_to_file_offset(image, 0x3FFFFFFF))


class Attribution(unittest.TestCase):
    def setUp(self):
        iram = pad_to(word(irc.RTC_BASE + 0x44), 0x20000, anchor=0x100)
        dram = pad_to(word(0x00000002), 0x200, anchor=0x1A0)
        self.data = build_image([(0x40374000, iram), (0x3FC98D00, dram)])
        self.image = irc.esp_app_segments(self.data)

    def test_reports_per_address_hex_and_absence(self):
        result = irc.attribute_signature(self.image, {
            "in_iram": 0x40374100, "runtime_only": 0x3FCA290C})
        self.assertEqual(result["attributed"], 1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["absent_from_image"], ["runtime_only"])
        self.assertEqual(result["addresses"]["in_iram"]["vaddr"], "0x40374100")
        self.assertEqual(result["addresses"]["in_iram"]["segment"], 0)
        self.assertFalse(result["addresses"]["runtime_only"]["attributed"])
        self.assertEqual(result["image_entry"], "0x403760d8")

    def test_tracer_signature_addresses_live_in_loaded_segments(self):
        result = irc.attribute_signature(self.image)
        self.assertGreaterEqual(result["attributed"], 4)
        self.assertIn("rtc_int_raw_poll", result["addresses"])


class LiteralSearch(unittest.TestCase):
    def test_only_aligned_occurrences_inside_segments(self):
        target = irc.RTC_BASE + 0x44
        blob = bytearray(0x40)
        blob[0x10:0x14] = word(target)          # aligned -> found
        blob[0x21:0x25] = word(target)          # misaligned -> ignored
        outside = build_image([(0x40374000, bytes(blob))])
        image = irc.esp_app_segments(outside)
        found = irc.find_literals(image, outside, {target})
        self.assertEqual([item["vaddr"] for item in found], [0x40374010])
        self.assertEqual(found[0]["word"], target)
        self.assertEqual(found[0]["file_offset"],
                         image["segments"][0]["file_offset"] + 0x10)

    def test_found_offsets_point_at_the_real_bytes(self):
        blob = pad_to(word(irc.RTC_BASE + 0x44), 0x100, anchor=0x40)
        data = build_image([(0x40374000, blob), (0x3FC98D00, bytes(0x40))])
        image = irc.esp_app_segments(data)
        found = irc.find_literals(image, data, {irc.RTC_BASE + 0x44})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["vaddr"], 0x40374040)
        self.assertEqual(int.from_bytes(
            data[found[0]["file_offset"]:found[0]["file_offset"] + 4], "little"),
            irc.RTC_BASE + 0x44)
        mapped = irc.vaddr_to_file_offset(image, 0x40374040)
        self.assertEqual(mapped["file_offset"], found[0]["file_offset"])

    def test_words_in_padding_are_not_reported(self):
        blob = pad_to(word(irc.RTC_BASE + 0x44), 0x40, anchor=0x20)
        data = build_image([(0x40374000, blob)]) + word(irc.RTC_BASE + 0x44)
        image = irc.esp_app_segments(data)
        found = irc.find_literals(image, data, {irc.RTC_BASE + 0x44})
        self.assertEqual(len(found), 1)


class L32RReach(unittest.TestCase):
    def test_boundaries(self):
        base = 0x4037CB2C & ~3
        self.assertTrue(irc.l32r_reach(base, base + irc.L32R_MIN_BYTES)["within_range"])
        self.assertTrue(irc.l32r_reach(base, base + irc.L32R_MAX_BYTES)["within_range"])
        self.assertFalse(irc.l32r_reach(base, base + irc.L32R_MIN_BYTES - 4)["within_range"])
        self.assertFalse(irc.l32r_reach(base, base + irc.L32R_MAX_BYTES + 4)["within_range"])

    def test_delta_is_measured_from_aligned_pc(self):
        reach = irc.l32r_reach(0x4037CB2F, 0x4037CB2C)
        self.assertEqual(reach["delta_bytes"], 0)
        self.assertEqual(reach["pc"], "0x4037cb2f")


class MiniTree:
    """A read-only lookalike of the QEMU tree, only the files this tool greps."""

    def __init__(self, root: Path, int_raw_served=False, rtc_irq_driven=False,
                 matrix_users=False, ipi_complete=True):
        self.root = root
        header = ["REG32(RTC_CNTL_OPTIONS0, 0x00)",
                  "REG32(RTC_CNTL_RESET_STATE, 0x38)",
                  "REG32(RTC_CNTL_DATE, 0x1fc)",
                  "ESP32_DEEPSLEEP_RESET = 5,"]
        if int_raw_served:
            header.append("REG32(RTC_CNTL_INT_RAW, 0x44)")
        read = "\n".join(
            ["static uint64_t esp32s3_rtc_cntl_read(void *opaque, hwaddr addr, unsigned int size)",
             "{", "    switch (addr) {",
             "    case A_RTC_CNTL_OPTIONS0:", "    case A_RTC_CNTL_RESET_STATE:"]
            + (["    case A_RTC_CNTL_INT_RAW:"] if int_raw_served else [])
            + ["    }", "    return 0;", "}"])
        write = ("static void esp32s3_rtc_cntl_write(void *opaque, hwaddr addr,\n"
                 "                                 uint64_t value, unsigned int size)\n"
                 "{ switch (addr) { case A_RTC_CNTL_OPTIONS0: } }")
        irq = ("    sysbus_init_irq(sbd, &s->irq);\n"
               + ("    qemu_set_irq(s->irq, level);\n" if rtc_irq_driven else ""))
        rtc_c = "\n".join([read, write, "static void init(void)\n{", irq, "}"])
        soc_lines = ["ETS_FROM_CPU_INTR0_SOURCE + i"] if ipi_complete else []
        if matrix_users:
            soc_lines.append("some file mentioning ETS_RTC_CORE_INTR_SOURCE")
        self._write("include/hw/misc/esp32s3_rtc_cntl.h", "\n".join(header))
        self._write("hw/misc/esp32s3_rtc_cntl.c", rtc_c)
        self._write("hw/xtensa/esp32s3.c", "\n".join(soc_lines))
        if ipi_complete:
            self._write("hw/xtensa/esp32s3_clk.c",
                        "case A_SYSTEM_CPU_INTR_FROM_CPU_0:\n"
                        "qemu_set_irq(s->irqs[index], 1);\n"
                        "qemu_set_irq(s->irqs[index], 0);\n")
            self._write("hw/xtensa/esp32s3_intc.c",
                        "#define INTMATRIX_UNINT_VALUE   6\n"
                        "ybx_from_cpu_asserted[i]++;\n"
                        "qemu_set_irq(s->outputs[i][int_index], level);\n")
            self._write("hw/xtensa/pic_cpu.c",
                        "if (env->sregs[INTSET] & mask) {\n"
                        "    cpu_interrupt(cs, CPU_INTERRUPT_HARD);\n}\n")

    def _write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


class ModelCoverage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ybx-0093-tree-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_unserviced_int_group_reads_as_zero(self):
        tree = MiniTree(self.tmp).root
        coverage = irc.rtc_model_coverage(tree)
        row = [item for item in coverage["touched"]
               if item["address"] == "0x60008044"][0]
        self.assertFalse(row["defined_in_model_header"])
        self.assertFalse(row["served_on_read"])
        self.assertEqual(row["guest_sees"], "hardwired 0")
        self.assertEqual(coverage["missing_int_group"],
                         ["0x60008040", "0x60008044", "0x60008048", "0x6000804c"])

    def test_defined_and_served_registers_are_not_reported_missing(self):
        coverage = irc.rtc_model_coverage(MiniTree(self.tmp).root)
        options = [item for item in coverage["touched"]
                   if item["address"] == "0x60008000"][0]
        self.assertTrue(options["served_on_read"])
        self.assertEqual(options["guest_sees"], "stored value")
        # regression: the case label must be matched without doubling RTC_CNTL
        self.assertEqual(options["symbols"], ["RTC_CNTL_OPTIONS0"])

    def test_serving_int_raw_removes_it_from_the_gap(self):
        coverage = irc.rtc_model_coverage(
            MiniTree(self.tmp, int_raw_served=True).root)
        self.assertEqual(coverage["missing_int_group"],
                         ["0x60008040", "0x60008048", "0x6000804c"])

    def test_unknown_function_raises(self):
        with self.assertRaises(ValueError):
            irc._switch_cases("nothing here", "esp32s3_rtc_cntl_read")


class IrqWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ybx-0093-wire-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_initialised_but_unconnected(self):
        wiring = irc.irq_wiring(MiniTree(self.tmp).root)
        self.assertTrue(wiring["rtc_cntl_irq_initialised"])
        self.assertEqual(wiring["rtc_cntl_irq_connected_in_soc"], [])
        self.assertEqual(wiring["wakeup_token_in_rtc_model"], [])
        self.assertFalse(wiring["rtc_source_can_raise_irq"])

    def test_matrix_user_is_detected(self):
        wiring = irc.irq_wiring(MiniTree(self.tmp).root)
        self.assertEqual(wiring["rtc_core_source_users"], [])
        wired = irc.irq_wiring(MiniTree(self.tmp / "alt", matrix_users=True).root)
        self.assertEqual(len(wired["rtc_core_source_users"]), 1)
        self.assertIn("ETS_RTC_CORE_INTR_SOURCE",
                      wired["rtc_core_source_users"][0])

    def test_driven_irq_line_flips_the_capability(self):
        wiring = irc.irq_wiring(MiniTree(self.tmp, rtc_irq_driven=True).root)
        self.assertTrue(wiring["rtc_source_can_raise_irq"])

    def test_missing_files_grep_empty(self):
        empty = Path(tempfile.mkdtemp(prefix="ybx-0093-none-"))
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        self.assertEqual(irc.grep_lines(empty / "nope.c", r"anything"), [])
        wiring = irc.irq_wiring(empty)
        self.assertFalse(wiring["rtc_cntl_irq_initialised"])


class IpiChain(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ybx-0093-ipi-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_complete_chain_reports_every_stage(self):
        ipi = irc.ipi_chain_presence(MiniTree(self.tmp).root)
        stages = {k: v for k, v in ipi.items()
                  if k != "counters_exported_for_runtime_check"}
        self.assertTrue(all(stages.values()), stages)
        self.assertIn("ybx-cpu{0,1}-from-cpu-default-route",
                      ipi["counters_exported_for_runtime_check"])

    def test_absent_chain_is_reported_as_absent(self):
        ipi = irc.ipi_chain_presence(MiniTree(self.tmp, ipi_complete=False).root)
        self.assertFalse(ipi["system_from_cpu_registers_modelled"])
        self.assertFalse(ipi["cpu_level_entry_in_pic"])


class HypothesisRanking(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ybx-0093-rank-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _inputs(self, **tree_kwargs):
        root = MiniTree(self.tmp, **tree_kwargs).root
        return (irc.rtc_model_coverage(root), irc.irq_wiring(root),
                irc.ipi_chain_presence(root))

    def test_ordering_and_vocabulary(self):
        rows = irc.rank_hypotheses(*self._inputs())
        self.assertEqual([row["rank"] for row in rows], list(range(1, 7)))
        for row in rows:
            self.assertTrue(row["falsifying_measurement"].strip())
            self.assertIsInstance(row["needs_qemu"], bool)
            self.assertTrue(row["claim"].strip())
            self.assertTrue(row["basis"])
        self.assertEqual(rows[0]["status"], "SUPPORTED_BY_SOURCE")
        self.assertEqual(rows[1]["status"], "SUPPORTED_BY_SOURCE")
        self.assertEqual(rows[2]["status"], "DEMOTE_TO_RUNTIME_CHECK")

    def test_h1_flips_when_the_model_services_int_raw(self):
        rows = irc.rank_hypotheses(*self._inputs(int_raw_served=True))
        self.assertEqual(rows[0]["status"], "REFUTED_BY_SOURCE")

    def test_h2_flips_once_the_rtc_source_has_a_wiring(self):
        coverage, wiring, ipi = self._inputs()
        self.assertEqual(
            irc.rank_hypotheses(coverage, wiring, ipi)[1]["status"],
            "SUPPORTED_BY_SOURCE")
        rows = irc.rank_hypotheses(*self._inputs(matrix_users=True))
        self.assertEqual(rows[1]["status"], "REFUTED_BY_SOURCE")
        wired = irc.rank_hypotheses(
            coverage, dict(wiring, rtc_cntl_irq_connected_in_soc=[
                "esp32s3.c:450: sysbus_connect_irq(SYS_BUS_DEVICE(&s->rtc_cntl), …)"]),
            ipi)
        self.assertEqual(wired[1]["status"], "REFUTED_BY_SOURCE")

    def test_h3_is_a_source_gap_when_the_chain_is_incomplete(self):
        rows = irc.rank_hypotheses(*self._inputs(ipi_complete=False))
        self.assertEqual(rows[2]["status"], "SUPPORTED_BY_SOURCE")

    def test_attribution_counts_reach_the_panel_hypothesis(self):
        data = build_image([(0x40374000, pad_to(word(irc.RTC_BASE + 0x44), 0x40, 0x20))])
        attribution = irc.attribute_signature(irc.esp_app_segments(data))
        rows = irc.rank_hypotheses(*self._inputs(), attribution=attribution)
        self.assertIn(f"{attribution['attributed']}/{attribution['total']}",
                      rows[4]["basis"][0])
        self.assertIn("not measured",
                      irc.rank_hypotheses(*self._inputs())[4]["basis"][0])


class ForcedWakeVariant(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ybx-0093-var-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        blob = pad_to(word(irc.RTC_BASE + 0x44), 0x800, anchor=0x7C)
        self.flash = self.tmp / "inkmod.bin"
        self.flash.write_bytes(build_image([(0x40374000, blob)], tail=64))
        self.vaddr = 0x40374000 + 0x7C

    def test_plan_selects_the_reachable_literal(self):
        data = self.flash.read_bytes()
        image = irc.esp_app_segments(data)
        literals = irc.find_literals(image, data, {irc.RTC_BASE + 0x44})
        plan = irc.variant_plan(image, literals, 0x40374100)
        self.assertTrue(plan["unique"])
        self.assertTrue(plan["candidates"][0]["reaches_poll_pc"])
        self.assertEqual(plan["candidates"][0]["replacement_word"],
                         f"0x{irc.RTC_BASE + irc.FORCED_WAKE_OFFSET:08x}")

    def test_plan_flags_an_unreachable_literal(self):
        data = self.flash.read_bytes()
        image = irc.esp_app_segments(data)
        literals = irc.find_literals(image, data, {irc.RTC_BASE + 0x44})
        plan = irc.variant_plan(image, literals, 0x4037CB2C)
        self.assertFalse(plan["candidates"][0]["reaches_poll_pc"])

    def test_variant_pokes_the_word_and_repairs_the_checksum(self):
        record = irc.build_variant(self.flash, self.tmp / "out", self.vaddr,
                                   irc.RTC_BASE + irc.FORCED_WAKE_OFFSET)
        self.assertEqual(record["word_before"], "0x60008044")
        self.assertEqual(record["word_after"],
                         f"0x{irc.RTC_BASE + irc.FORCED_WAKE_OFFSET:08x}")
        # the app image carries a simple XOR checksum; leaving it stale makes
        # the bootloader reject the image and the guest never reaches the app
        self.assertEqual(record["checksum_before"], "0x00")
        self.assertEqual(record["checksum_after"],
                         f"0x{0x44 ^ irc.FORCED_WAKE_OFFSET:02x}")
        self.assertEqual(record["changed_offsets"],
                         [record["file_offset"], record["checksum_position"]])
        self.assertEqual(record["bytes_changed"], 2)
        self.assertEqual(record["input_sha256"], irc.sha256_file(self.flash))
        variant = Path(record["variant_path"])
        self.assertEqual(variant.name, "inkmod-forced-wake-x4pro-flash-16m.bin")
        self.assertNotEqual(record["variant_sha256"], record["input_sha256"])
        self.assertEqual(variant.stat().st_size, self.flash.stat().st_size)
        original = self.flash.read_bytes()
        patched = variant.read_bytes()
        self.assertEqual([hex(i) for i, (a, b) in enumerate(zip(original, patched))
                          if a != b],
                         [record["file_offset"], record["checksum_position"]])

    def test_variant_refuses_an_image_without_room_for_the_checksum(self):
        blob = pad_to(word(irc.RTC_BASE + 0x44), 0x800, anchor=0x7C)
        bare = self.tmp / "bare.bin"
        bare.write_bytes(build_image([(0x40374000, blob)]))
        with self.assertRaises(ValueError):
            irc.build_variant(bare, self.tmp / "out", self.vaddr,
                              irc.RTC_BASE + irc.FORCED_WAKE_OFFSET)


    def test_variant_refuses_to_poke_a_word_that_is_not_int_raw(self):
        with self.assertRaises(ValueError):
            irc.build_variant(self.flash, self.tmp / "out", self.vaddr + 4,
                              irc.RTC_BASE + irc.FORCED_WAKE_OFFSET)

    def test_variant_refuses_to_poke_outside_a_loaded_segment(self):
        with self.assertRaises(ValueError):
            irc.build_variant(self.flash, self.tmp / "out", 0x3FCA290C,
                              irc.RTC_BASE + irc.FORCED_WAKE_OFFSET)


class ChecksumPosition(unittest.TestCase):
    def test_unaligned_image_end_pads_up_to_the_block_last_byte(self):
        # walk_end 0x10000 + 24 + 8 + 0x7FC = 0x1081c -> last byte 0x1081f
        blob = pad_to(word(1), 0x7FC, anchor=0)
        image = irc.esp_app_segments(build_image([(0x40374000, blob)]))
        self.assertEqual(image["walk_end"], 0x1081C)
        self.assertEqual(irc.checksum_position(image), 0x1081F)

    def test_aligned_image_end_gets_a_block_of_its_own(self):
        # walk_end 0x10000 + 24 + 8 + 0x800 = 0x10820, already a block boundary
        blob = pad_to(word(1), 0x800, anchor=0)
        image = irc.esp_app_segments(build_image([(0x40374000, blob)]))
        self.assertEqual(image["walk_end"] % 16, 0)
        self.assertEqual(irc.checksum_position(image), image["walk_end"] + 15)

    @unittest.skipUnless(Path(irc.DEFAULT_FLASH).is_file(),
                         "inkMOD 1.1.10 flash image not present")
    def test_matches_the_value_the_guest_itself_reported(self):
        """Measured 2026-11-01: the bootloader said Calculated 0x4f read 0x33."""
        data = Path(irc.DEFAULT_FLASH).read_bytes()
        image = irc.esp_app_segments(data)
        position = irc.checksum_position(image)
        self.assertEqual(position, 0x44912F)
        stored = data[position]
        self.assertEqual(stored, 0x33)
        self.assertEqual(stored ^ 0x44 ^ 0x38, 0x4F)


class RunbookSafety(unittest.TestCase):
    def test_container_invocation_is_isolated_and_read_only(self):
        record = {"variant_path": "/tmp/x/inkmod-forced-wake-x4pro-flash-16m.bin"}
        text = irc.runbook(Path("/fw/inkmod-v1.1.10-x4pro-flash-16m.bin"), record)
        self.assertIn("--rm", text)
        self.assertIn("--network none", text)
        # the daemon rejects "linux-arm64"; the runbook must emit the valid form
        self.assertIn("--platform linux/arm64", text)
        self.assertNotIn("--platform linux-arm64", text)
        for mount in (":/qemu:ro", ":/tools:ro", ":/fw:ro", ":/sd:ro",
                      ":/roms:ro"):
            self.assertIn(mount, text)
        self.assertIn("ybx-qemu-builder:20261029", text)
        self.assertNotIn("browser", text.replace("browser container is never", ""))
        for label in ("factory-control", "inkmod-pristine", "inkmod-forced-wake"):
            self.assertIn(label, text)
        self.assertIn("probe_x4pro_inkmod_trace.py", text)
        # the only writable mount is the work dir holding the variant + runs
        self.assertEqual(text.count("bash -lc"), 1)
        self.assertEqual(text.count("-v /tmp/x"), 1)
        self.assertIn("-v /tmp/x:/work", text)
        self.assertIn("H1 CONFIRMED", text)
        self.assertIn("the vendor build-ybx/esp32s3_rev0_rom.bin boots nothing",
                      text)


class ReportComparison(unittest.TestCase):
    def test_absent_report_leaves_hypotheses_untouched(self):
        self.assertIsNone(irc.load_tracer_report(None))
        rows = [{"rank": 1}]
        self.assertEqual(irc.compare_against_report(rows, None), rows)

    def test_recorded_run_is_attached_to_every_row(self):
        tmp = Path(tempfile.mkdtemp(prefix="ybx-0093-rep-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        report = tmp / "report.json"
        report.write_text(json.dumps({
            "verdict": {"verdict": "deep_sleep_poll_stalled"},
            "epd": {"commands": 0},
            "intmatrix": {"ybx-cpu1-unrouted": 0},
            "clock": {"ybx-cpu-intr-2-sets": 3},
            "inkmod_config": {"wakeup_triggers": 2}}))
        loaded = irc.load_tracer_report(report)
        rows = irc.compare_against_report([{"rank": 1}, {"rank": 2}], loaded)
        for row in rows:
            self.assertEqual(row["recorded_run"]["verdict"],
                             "deep_sleep_poll_stalled")
            self.assertEqual(row["recorded_run"]["wakeup_triggers"], 2)
            self.assertEqual(row["recorded_run"]["clock"]["ybx-cpu-intr-2-sets"], 3)


class CommandLine(unittest.TestCase):
    def test_analyze_writes_a_machine_readable_record(self):
        tmp = Path(tempfile.mkdtemp(prefix="ybx-0093-cli-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        blob = pad_to(word(irc.RTC_BASE + 0x44), 0x2000, anchor=0x100)
        flash = tmp / "inkmod.bin"
        flash.write_bytes(build_image([(0x40374000, blob)]))
        tree = MiniTree(tmp / "tree").root
        out = tmp / "cause.json"
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(stderr):
            code = irc.main(["analyze", "--flash", str(flash), "--tree",
                             str(tree), "--poll-pc", "0x40374100",
                             "--out", str(out)])
        self.assertEqual(code, 0)
        result = json.loads(out.read_text())
        self.assertEqual(result["tool"], "inkmod_root_cause")
        self.assertEqual(result["image"]["segment_count"], 1)
        self.assertTrue(result["forced_wake_plan"]["unique"])
        self.assertEqual(len(result["hypotheses"]), 6)
        self.assertTrue(result["inputs"]["flash_sha256"])


@unittest.skipUnless(Path(irc.DEFAULT_TREE, irc.RTC_SOURCE).is_file(),
                     "live QEMU tree not present")
class LiveTreeFacts(unittest.TestCase):
    """The measured state of the audited 26-patch tree on this disk."""

    @classmethod
    def setUpClass(cls):
        cls.coverage = irc.rtc_model_coverage(Path(irc.DEFAULT_TREE))
        cls.wiring = irc.irq_wiring(Path(irc.DEFAULT_TREE))
        cls.ipi = irc.ipi_chain_presence(Path(irc.DEFAULT_TREE))

    def test_rtc_int_group_is_absent_and_reads_zero(self):
        self.assertEqual(self.coverage["missing_int_group"],
                         ["0x60008040", "0x60008044", "0x60008048", "0x6000804c"])
        self.assertNotIn("A_RTC_CNTL_INT_RAW", self.coverage["read_cases"])
        row = [item for item in self.coverage["touched"]
               if item["address"] == "0x60008044"][0]
        self.assertEqual(row["guest_sees"], "hardwired 0")

    def test_modelled_registers_stay_modelled(self):
        for address in ("0x60008000", "0x60008038"):
            row = [item for item in self.coverage["touched"]
                   if item["address"] == address][0]
            self.assertEqual(row["guest_sees"], "stored value", address)

    def test_no_rtc_interrupt_reaches_any_cpu(self):
        self.assertTrue(self.wiring["rtc_cntl_irq_initialised"])
        self.assertEqual(self.wiring["rtc_cntl_irq_connected_in_soc"], [])
        self.assertEqual(self.wiring["rtc_core_source_users"], [])
        self.assertFalse(self.wiring["rtc_source_can_raise_irq"])
        self.assertEqual(self.wiring["wakeup_token_in_soc"], [])

    def test_cross_core_ipi_stages_are_all_present(self):
        stages = {k: v for k, v in self.ipi.items()
                  if k != "counters_exported_for_runtime_check"}
        self.assertTrue(all(stages.values()), stages)

    def test_hypothesis_ordering_for_the_live_tree(self):
        rows = irc.rank_hypotheses(self.coverage, self.wiring, self.ipi)
        self.assertEqual(
            [row["status"] for row in rows],
            ["SUPPORTED_BY_SOURCE", "SUPPORTED_BY_SOURCE",
             "DEMOTE_TO_RUNTIME_CHECK", "UNTESTED_GEOMETRY", "SUBORDINATE",
             "PENDING_RUNTIME"])


@unittest.skipUnless(Path(irc.DEFAULT_FLASH).is_file(),
                     "inkMOD 1.1.10 flash image not present")
class LiveImageFacts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.flash = Path(irc.DEFAULT_FLASH)
        cls.data = cls.flash.read_bytes()
        cls.image = irc.esp_app_segments(cls.data)
        cls.literals = irc.find_literals(
            cls.image, cls.data,
            {irc.RTC_BASE + off for off in irc.RTC_INT_GROUP})

    def test_header_identity(self):
        self.assertEqual(self.image["entry_addr"], 0x403760D8)
        self.assertEqual(self.image["chip_id"], 0x0009)
        self.assertEqual(self.image["segment_count"], 8)

    def test_signature_addresses_are_in_the_image_except_runtime_bss(self):
        attribution = irc.attribute_signature(self.image)
        self.assertEqual(attribution["absent_from_image"],
                         ["inkmod_ipc_finish_cmd"])
        self.assertEqual(attribution["attributed"], attribution["total"] - 1)

    def test_rtc_int_raw_literal_is_unique_and_reaches_the_poll_pc(self):
        plan = irc.variant_plan(self.image, self.literals, 0x4037CB2C)
        self.assertTrue(plan["unique"], plan)
        candidate = plan["candidates"][0]
        self.assertEqual(candidate["literal_vaddr"], "0x40374b7c")
        self.assertEqual(candidate["file_offset"], "0x20d3fc")
        self.assertTrue(candidate["reaches_poll_pc"])
        self.assertGreater(candidate["delta_bytes"], irc.L32R_MIN_BYTES)

    def test_the_app_hash_rule_reproduces_the_digest_the_guest_validates(self):
        """Same image the boot-loop reported "Image hash failed" on."""
        checked = irc.verify_app_hash(self.data)
        self.assertEqual(checked["hash_position"], "0x449130")
        self.assertTrue(checked["present"], checked)
        self.assertTrue(checked["match"], checked)

    def test_every_forced_wake_substitute_is_a_modelled_register(self):
        coverage = irc.rtc_model_coverage(Path(irc.DEFAULT_TREE))
        served = set(coverage["read_cases"])
        for offset, _ in irc.WAKE_SUBSTITUTES:
            row = [item for item in coverage["touched"]
                   if item["offset"] == f"0x{offset:02x}"]
            self.assertTrue(row, f"0x{offset:02x} missing from the table")
            self.assertTrue(row[0]["defined_in_model_header"], row)
            self.assertTrue(row[0]["served_on_read"], row)
            self.assertTrue(served & {f"A_{name}" for name in row[0]["symbols"]},
                            row)


class AppHashRepair(unittest.TestCase):
    """The second integrity guard, on synthetic images sized like a real slot."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="inkmod-hash-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def sealed(self, segments):
        data = bytearray(build_image(segments))
        image = irc.esp_app_segments(bytes(data))
        position = irc.checksum_position(image) + 1
        while len(data) < position + irc.HASH_BYTES:
            data.extend(bytes(16))
        digest = hashlib.sha256(bytes(data[irc.APP_OFFSET:position])).digest()
        data[position:position + irc.HASH_BYTES] = digest
        return bytes(data), irc.app_hash_position(image)

    def test_hash_position_follows_the_checksum_byte(self):
        data = build_image([(0x40374000, pad_to(word(irc.RTC_BASE + 0x44), 0x800))])
        image = irc.esp_app_segments(data)
        self.assertEqual(irc.app_hash_position(image),
                         irc.checksum_position(image) + 1)

    def test_rule_matches_a_digest_computed_over_the_same_region(self):
        data, position = self.sealed(
            [(0x40374000, pad_to(word(irc.RTC_BASE + 0x44), 0x800))])
        checked = irc.verify_app_hash(data)
        self.assertEqual(checked["hash_position"], f"0x{position:x}")
        self.assertTrue(checked["present"], checked)
        self.assertTrue(checked["match"], checked)

    def test_an_all_zero_trailer_reads_as_no_hash_at_all(self):
        checked = irc.verify_app_hash(
            build_image([(0x40374000, pad_to(word(irc.RTC_BASE + 0x44), 0x800))],
                        tail=0x2000))
        self.assertFalse(checked["present"], checked)

    def test_variant_pokes_the_word_checksum_and_hash_together(self):
        data, position = self.sealed(
            [(0x40374000, pad_to(word(irc.RTC_BASE + 0x44), 0x800, anchor=0x100))])
        flash = self.tmp / "sealed.bin"
        flash.write_bytes(data)
        record = irc.build_variant(flash, self.tmp / "out", 0x40374100,
                                   irc.RTC_BASE + 0x38)
        self.assertEqual(record["app_hash"]["state"], "repaired")
        self.assertEqual(record["checksum_after"], f"0x{0x00 ^ 0x7c:02x}")
        patched = Path(record["variant_path"]).read_bytes()
        self.assertTrue(irc.verify_app_hash(patched)["match"],
                        irc.verify_app_hash(patched))
        literal = int(record["file_offset"], 16)
        checksum = int(record["checksum_position"], 16)
        changed = {int(item, 16) for item in record["changed_offsets"]}
        allowed = {literal, checksum} | set(
            range(position, position + irc.HASH_BYTES))
        self.assertTrue(changed <= allowed, sorted(changed - allowed))
        self.assertIn(checksum, changed)
        self.assertIn(literal, changed)

    def test_variant_refuses_a_digest_the_rule_does_not_explain(self):
        data, position = self.sealed(
            [(0x40374000, pad_to(word(irc.RTC_BASE + 0x44), 0x800, anchor=0x100))])
        broken = bytearray(data)
        broken[position] ^= 0xFF
        flash = self.tmp / "broken-hash.bin"
        flash.write_bytes(bytes(broken))
        with self.assertRaises(ValueError):
            irc.build_variant(flash, self.tmp / "out", 0x40374100,
                              irc.RTC_BASE + 0x38)


class ForcedWakeTarget(unittest.TestCase):
    """Which RTC offset the poll can be redirected to, decided by measurement."""

    def rows(self, **overrides):
        table = []
        for offset, name in irc.WAKE_SUBSTITUTES:
            row = {"offset": f"0x{offset:02x}",
                   "address": f"0x{ifw.RTC_BASE + offset:08x}",
                   "description": name, "values": [0], "sampled": True,
                   "changing": False, "wake_bits_set": False, "nonzero": False}
            row.update(overrides.get(f"0x{offset:02x}", {}))
            table.append(row)
        return table

    def test_every_substitute_is_named_by_the_model_header(self):
        offsets = {offset for offset, _ in irc.WAKE_SUBSTITUTES}
        self.assertNotIn(0x44, offsets)
        self.assertTrue(offsets & {0x10, 0x14}, offsets)

    def test_a_changing_value_beats_a_merely_nonzero_one(self):
        table = self.rows(**{"0x10": {"nonzero": True},
                             "0x14": {"values": [4, 90], "changing": True,
                                      "nonzero": True}})
        self.assertEqual(ifw.choose_substitute(table), 0x14)

    def test_wake_bits_beat_a_value_that_is_only_nonzero(self):
        table = self.rows(**{"0x38": {"values": [0x100], "nonzero": True},
                             "0x50": {"values": [0x3], "wake_bits_set": True,
                                      "nonzero": True}})
        self.assertEqual(ifw.choose_substitute(table), 0x50)

    def test_nothing_selected_when_the_model_reads_zero_everywhere(self):
        self.assertIsNone(ifw.choose_substitute(self.rows()))

    def test_candidate_table_survives_a_failed_probe_round(self):
        table = ifw.candidate_table([])
        self.assertEqual(len(table), len(irc.WAKE_SUBSTITUTES))
        for row in table:
            self.assertFalse(row["sampled"])
            self.assertFalse(row["nonzero"])
        self.assertIsNone(ifw.choose_substitute(table))

    def test_unreadable_literal_is_reported_not_raised(self):
        def explode(*args, **kwargs):
            raise RuntimeError("xp returned no words")
        self.assertTrue(ifw.read_poll_literal(explode, 0x40374B7C)
                        .startswith("unreadable:"))

    def test_readable_literal_is_reported_as_a_word(self):
        def reply(*args, **kwargs):
            return {"return": "40374b7c: 0x60008044 0x60008038\n"}
        self.assertEqual(ifw.read_poll_literal(reply, 0x40374B7C), "0x60008044")


if __name__ == "__main__":
    unittest.main()
