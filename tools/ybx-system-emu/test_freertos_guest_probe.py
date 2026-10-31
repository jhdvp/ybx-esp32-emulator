"""Host tests for the read-only FreeRTOS guest-RAM decoder.

The synthetic image uses the addresses recorded in the real factory guest runs
(render TCB 0x3fcec420, blocked queue 0x3fcec294, xDelayedTaskList1 0x3fcac00c)
so the layout, not just the arithmetic, is pinned to evidence.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import struct
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import freertos_guest_probe as rtos
import probe_x4pro_freertos_wake as wake_probe

RENDER_TCB = 0x3FCEC420
WORKER_TCB = 0x3FCEC900
BLOCKED_QUEUE = 0x3FCEC294
DELAYED1 = rtos.KERNEL["xDelayedTaskList1"]
DELAYED2 = rtos.KERNEL["xDelayedTaskList2"]
READY3 = rtos.KERNEL["pxReadyTasksLists"] + 3 * rtos.LIST_SIZE
SUSPENDED = rtos.KERNEL["xSuspendedTaskList"]
TERMINATION = rtos.KERNEL["xTasksWaitingTermination"]
# Scratch DRAM used only for forged list items, so a walk reaches an item
# whose owner is a duplicate, a nonsense pointer, or a TCB with no name.
FORGED_ITEM = 0x3FCED700
# Scratch DRAM for forged TCBs, clear of the recorded render task, the blocked
# queue and the item region above.
FORGED_TCB = 0x3FCEF000
# Scratch DRAM for a List_t forged by a test. It sits below FORGED_ITEM so the
# header never overlaps the items it points at, which is what let a 65-item
# ring silently stop at 9 items.
SCRATCH_LIST = 0x3FCED600
READY0 = rtos.KERNEL["pxReadyTasksLists"]
READY1 = READY0 + rtos.LIST_SIZE


def put_words(mem, address, *values):
    for offset, value in enumerate(values):
        mem[address + 4 * offset] = value & 0xFFFFFFFF


def forge_task(mem, tcb, name, priority, state_list, event_list, value=0,
               state=0):
    """Write one TCB-shaped record at `tcb`.

    Mirrors what the kernel leaves behind: a name, priorities, counters and
    two list items whose `pvContainer` says where the task is parked.
    """
    raw = name.encode().ljust(16, b"\0")[:16]
    put_words(mem, tcb + rtos.TCB["ux_priority"], priority)
    put_words(mem, tcb + rtos.TCB["ux_base_priority"], priority)
    put_words(mem, tcb + rtos.TCB["ux_mutexes_held"], 1)
    put_words(mem, tcb + rtos.TCB["ux_tcb_number"], 7)
    put_words(mem, tcb + rtos.TCB["x_core_id"], 1)
    put_words(mem, tcb + rtos.TCB["ul_run_time_counter"], 1234)
    put_words(mem, tcb + rtos.TCB["ul_notified_value"], value)
    put_words(mem, tcb + rtos.TCB["uc_notify_state"], state)
    for offset in range(0, 16, 4):
        put_words(mem, tcb + rtos.TCB["pc_task_name"] + offset,
                  int.from_bytes(raw[offset:offset + 4], "little"))
    state_item = tcb + rtos.TCB["x_state_list_item"]
    event_item = tcb + rtos.TCB["x_event_list_item"]
    for item, container in ((state_item, state_list),
                            (event_item, event_list)):
        put_words(mem, item + rtos.LIST_ITEM["x_item_value"], 5)
        put_words(mem, item + rtos.LIST_ITEM["px_next"], item)
        put_words(mem, item + rtos.LIST_ITEM["px_previous"], item)
        put_words(mem, item + rtos.LIST_ITEM["pv_owner"], tcb)
        put_words(mem, item + rtos.LIST_ITEM["pv_container"], container)


def relink(mem, list_address, items):
    """Rewire one initialised List_t so it holds exactly `items`.

    `items` is a list of (item_address, owner_tcb) in walk order; the kernel
    keeps a list circular through xListEnd, which is what this reproduces.
    """
    end = list_address + rtos.LIST["x_list_end"]
    ring = [end] + [address for address, _owner in items] + [end]
    for position, (address, owner) in enumerate(items):
        mem[address + rtos.LIST_ITEM["x_item_value"]] = 5
        mem[address + rtos.LIST_ITEM["px_next"]] = ring[position + 2]
        mem[address + rtos.LIST_ITEM["px_previous"]] = ring[position]
        mem[address + rtos.LIST_ITEM["pv_owner"]] = owner
        mem[address + rtos.LIST_ITEM["pv_container"]] = list_address
    mem[list_address] = len(items) & 0xFFFFFFFF
    mem[list_address + rtos.LIST["px_index"]] = end
    mem[end] = 0xFFFFFFFF
    mem[end + 4] = ring[1]
    mem[end + 8] = ring[-2]


def build_image(notify_value=1, notify_state=2, render_state_list=DELAYED1,
                render_event_list=BLOCKED_QUEUE +
                rtos.QUEUE["x_tasks_waiting_to_receive"],
                epd_handle=BLOCKED_QUEUE, current_tcb=WORKER_TCB,
                busy_input=0x609B, scheduler=1, render_wake_tick=None,
                tick_count=0, initialise_extra=()):
    """Little-endian word map of a minimal, self-consistent FreeRTOS guest."""
    mem = {}

    def put(address, *values):
        put_words(mem, address, *values)

    forge_task(mem, RENDER_TCB, "ActivityManagerRender", 3,
               render_state_list, render_event_list, notify_value,
               notify_state)
    forge_task(mem, WORKER_TCB, "LoopTask", 3, READY3, 0, 0, 0)

    # Kernel statics: list membership follows the task containers above, so
    # the image is self-consistent the way the kernel leaves it.
    put(rtos.KERNEL["xSchedulerRunning"], scheduler)
    put(rtos.KERNEL["xTickCount"], tick_count)
    put(rtos.KERNEL["xNextTaskUnblockTime"],
        max(tick_count, render_wake_tick or 0))
    put(rtos.KERNEL["pxCurrentTCBs"], current_tcb, current_tcb)
    put(rtos.KERNEL["pxDelayedTaskList"], DELAYED1)
    if render_wake_tick is not None:
        # The wake tick is only meaningful on a delayed list; everywhere else
        # the kernel leaves the old item value behind (tasks.c:6381-6386).
        put(RENDER_TCB + rtos.TCB["x_state_list_item"] +
            rtos.LIST_ITEM["x_item_value"], render_wake_tick)
    members_by_list = {}
    for tcb, container in ((RENDER_TCB, render_state_list),
                           (WORKER_TCB, READY3)):
        if container:
            members_by_list.setdefault(container, []).append(
                tcb + rtos.TCB["x_state_list_item"])
    lists = ([rtos.KERNEL["xPendingReadyList"] + core * rtos.LIST_SIZE
              for core in range(rtos.CORES)] +
             [rtos.KERNEL["xDelayedTaskList2"], DELAYED1] +
             [rtos.KERNEL["pxReadyTasksLists"] + priority * rtos.LIST_SIZE
              for priority in range(rtos.MAX_PRIORITIES)] +
             # A list a task actually points at is initialised by definition;
             # the rest stay zeroed, as they are before the scheduler starts.
             list(members_by_list) + list(initialise_extra))
    for address in lists:
        members = members_by_list.get(address, [])
        end = address + rtos.LIST["x_list_end"]
        put(address, len(members), end)
        put(end, 0xFFFFFFFF, members[0] if members else end, end)

    # The binary semaphore the render task is parked on.
    receive = BLOCKED_QUEUE + rtos.QUEUE["x_tasks_waiting_to_receive"]
    send = BLOCKED_QUEUE + rtos.QUEUE["x_tasks_waiting_to_send"]
    put(BLOCKED_QUEUE + rtos.QUEUE["ux_messages_waiting"], 0)
    put(BLOCKED_QUEUE + rtos.QUEUE["ux_length"], 1)
    put(BLOCKED_QUEUE + rtos.QUEUE["ux_item_size"], 0)
    put(BLOCKED_QUEUE + rtos.QUEUE["ux_queue_number"], 9)
    put(BLOCKED_QUEUE + rtos.QUEUE["uc_queue_type"], 3)
    for address, member in ((receive, RENDER_TCB + rtos.TCB["x_event_list_item"]
                             if render_event_list == receive else 0),
                            (send, 0)):
        end = address + rtos.LIST["x_list_end"]
        put(address, 1 if member else 0, end)
        put(end, 0xFFFFFFFF, member if member else end, end)

    put(rtos.EPD_REFRESH_DONE_HANDLE, epd_handle)
    put(rtos.GPIO_IN_REG, busy_input)
    put(rtos.GPIO_STATUS_REG, 0)
    put(rtos.GPIO_ENABLE_REG, 0x47B36)
    put(rtos.GPIO_PIN6_CFG_REG, 0)
    return mem


COMMAND = re.compile(r"^xp /(\d+)wx 0x([0-9a-fA-F]{8})$")


def fake_qmp(mem, words_per_line=4, holes=False):
    """QMP stand-in for `xp /Nwx`, wrapped every `words_per_line` words.

    Like the real emulator it answers for any mapped address, so unmodelled
    words read as 0; `holes=True` rejects every read to simulate an
    unreachable guest.
    """
    calls = []

    def mapped(address):
        return (0x3FC88000 <= address < 0x3FD00000 or
                0x60004000 <= address < 0x60005000)

    def qmp(command, arguments):
        calls.append(arguments["command-line"])
        assert command == "human-monitor-command"
        match = COMMAND.match(arguments["command-line"])
        base, count = int(match.group(2), 16), int(match.group(1))
        lines, offset = [], 0
        while offset < count:
            address = base + 4 * offset
            values = []
            for index in range(min(words_per_line, count - offset)):
                item = address + 4 * index
                if item in mem:
                    values.append(mem[item])
                elif holes or not mapped(item):
                    return {"error": {"class": "GenericError",
                                      "desc": "Invalid access"}}
                else:
                    values.append(0)
            lines.append(f"{address:016x}: " + " ".join(
                f"0x{value:08x}" for value in values))
            offset += len(values)
        return {"return": "\n".join(lines) + "\n"}

    qmp.calls = calls
    return qmp


class HmpWordParsingTests(unittest.TestCase):
    def test_single_word(self):
        self.assertEqual(rtos.parse_xp_words(
            "0000000060004044: 0x00000000\n", 0x60004044, 1), [0])

    def test_wrapped_lines_keep_word_placement(self):
        reply = ("000000003fcac214: 0x3fcec900 0x3fcec900\n"
                 "000000003fcac21c: 0x00000001 0x00000002\n")
        self.assertEqual(
            rtos.parse_xp_words(reply, 0x3FCAC214, 4),
            [0x3FCEC900, 0x3FCEC900, 1, 2])

    def test_error_reply_is_not_a_zero(self):
        self.assertIsNone(rtos.parse_xp_words(
            {"error": {"class": "GenericError"}}, 0x3FCAC214, 1))

    def test_incomplete_request_returns_none(self):
        self.assertIsNone(rtos.parse_xp_words(
            "000000003fcac214: 0x00000001\n", 0x3FCAC214, 4))


class GuestRamTests(unittest.TestCase):
    def test_read_errors_are_recorded_not_fabricated(self):
        ram = rtos.GuestRam(fake_qmp({}, holes=True))
        self.assertIsNone(ram.u32(0x3FCAC214))
        self.assertTrue(ram.errors)

    def test_repeated_reads_are_cached(self):
        qmp = fake_qmp(build_image())
        ram = rtos.GuestRam(qmp)
        first = ram.u32(rtos.KERNEL["xSchedulerRunning"])
        second = ram.u32(rtos.KERNEL["xSchedulerRunning"])
        self.assertEqual(first, 1)
        self.assertEqual(second, 1)
        self.assertEqual(len(qmp.calls), 1)


class RenderTaskSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.qmp = fake_qmp(build_image())
        self.snap = rtos.snapshot(self.qmp)

    def test_task_is_discovered_without_a_hardcoded_tcb(self):
        self.assertTrue(self.snap["available"], self.snap.get("read_errors"))
        task = self.snap["render_task"]
        self.assertEqual(task["tcb"], hex(RENDER_TCB))
        # configMAX_TASK_NAME_LEN is 16, so the kernel stores 16 characters.
        self.assertEqual(task["name"], "ActivityManagerR")
        self.assertTrue(rtos.task_matches(task["name"],
                                         "ActivityManagerRender"))
        self.assertEqual(task["ux_priority"], 3)
        self.assertEqual(task["uc_notify_state_meaning"],
                         "notification-recorded-not-consumed")
        self.assertIn({"tcb": hex(WORKER_TCB), "name": "LoopTask"},
                      [{"tcb": row["tcb"], "name": row["name"]}
                       for row in self.snap["tasks"]])

    def test_running_render_task_is_discovered_from_px_current_tcbs(self):
        """A task caught on a CPU is on no ready/delayed list."""
        snap = rtos.snapshot(fake_qmp(build_image(
            render_state_list=0, render_event_list=0,
            current_tcb=RENDER_TCB)))
        self.assertTrue(snap["available"], snap.get("reason"))
        self.assertEqual(snap["render_task"]["tcb"], hex(RENDER_TCB))
        found = {row["tcb"]: row["found_in"] for row in snap["tasks"]}
        self.assertEqual(found[hex(RENDER_TCB)], "running-core-0")

    def test_blocked_queue_is_found_from_the_event_list_item(self):
        block = self.snap["render_task_block"]
        self.assertTrue(block["state_is_delayed"])
        self.assertTrue(block["blocked_on_queue"])
        self.assertEqual(block["blocked_queue"]["kind"], "binary-semaphore")
        self.assertEqual(block["blocked_queue"]["ux_messages_waiting"], 0)
        self.assertEqual(block["blocked_queue"]["ux_length"], 1)
        self.assertEqual(block["blocked_queue"]["ux_item_size"], 0)
        waiters = block["blocked_queue"]["waiters_receiving"]["walked"]
        self.assertEqual([item["name"] for item in waiters],
                         ["ActivityManagerR"])

    def test_blocked_queue_matches_epd_refresh_done(self):
        self.assertTrue(self.snap["render_task_block"]
                        ["blocked_queue_matches_epd_refresh_done"])

    def test_state_and_event_lists_are_named_from_kernel_statics(self):
        task = self.snap["render_task"]
        self.assertEqual(task["state_item"]["container_kind"],
                         "delayed-list-1")
        self.assertEqual(
            task["event_item"]["container_kind"],
            f"queue@{BLOCKED_QUEUE:08x}.waiting-to-receive:binary-semaphore")

    def test_busy_line_reads_low_with_no_gpio_edge_latched(self):
        """Recorded guest: GPIO_IN_REG=0x609b, GPIO_STATUS_REG=0.

        With an active-low BUSY pin that level means "still working", and no
        edge was ever latched, so the BUSY completion interrupt never ran.
        """
        busy = self.snap["epd_busy_gpio"]
        self.assertEqual(busy["input_reg"], hex(0x609B))
        self.assertEqual(busy["input_bit"], 0)
        self.assertEqual(busy["interrupt_status_bit"], 0)

    def test_semaphore_token_is_empty(self):
        self.assertEqual(self.snap["render_task_block"]["blocked_queue"]
                         ["ux_messages_waiting"], 0)

    def test_binary_semaphore_union_is_not_decoded_as_a_mutex_holder(self):
        self.assertIsNone(self.snap["render_task_block"]["blocked_queue"]
                          ["x_mutex_holder"])

    def test_garbage_queue_base_is_refused_not_guessed(self):
        ram = rtos.GuestRam(fake_qmp(build_image()))
        self.assertIsNone(rtos.read_queue(ram, 0x3FCABF8C))

    def test_unreachable_guest_is_marked_unavailable(self):
        snap = rtos.snapshot(fake_qmp({}, holes=True))
        self.assertFalse(snap["available"])
        self.assertTrue(snap["reason"])
        self.assertIsNone(snap["render_task"])
        self.assertTrue(snap["read_errors"])

    def test_zeroed_memory_is_not_decoded_as_a_running_kernel(self):
        snap = rtos.snapshot(fake_qmp({}, words_per_line=1))
        self.assertFalse(snap["available"])
        self.assertEqual(snap["task_count"], 0)
        self.assertIsNone(snap["render_task_block"])


class RenderWakeAssessmentTests(unittest.TestCase):
    def test_latched_notification_without_wake_is_not_a_wake(self):
        before = rtos.snapshot(fake_qmp(build_image(
            notify_value=0, notify_state=0)))
        after = rtos.snapshot(fake_qmp(build_image()))
        verdict = rtos.assess_render_wake(before, after)
        self.assertTrue(verdict["available"])
        self.assertTrue(verdict["notification_latched"])
        self.assertTrue(verdict["notification_state_recorded"])
        self.assertTrue(verdict["blocked_object_is_epd_refresh_done"])
        self.assertTrue(verdict["still_parked_in_delayed_list"])
        self.assertFalse(verdict["render_task_woken"])

    def test_moved_to_ready_list_counts_as_wake(self):
        after = rtos.snapshot(fake_qmp(build_image(
            notify_value=0, notify_state=0,
            render_state_list=rtos.KERNEL["pxReadyTasksLists"] +
            3 * rtos.LIST_SIZE,
            render_event_list=0)))
        before = rtos.snapshot(fake_qmp(build_image()))
        verdict = rtos.assess_render_wake(before, after)
        self.assertTrue(verdict["render_task_woken"])
        self.assertFalse(verdict["still_parked_in_delayed_list"])

    def test_running_on_a_core_counts_as_wake(self):
        after = rtos.snapshot(fake_qmp(build_image(
            notify_value=0, notify_state=0,
            render_state_list=rtos.KERNEL["xPendingReadyList"],
            render_event_list=0, current_tcb=RENDER_TCB)))
        verdict = rtos.assess_render_wake(rtos.snapshot(
            fake_qmp(build_image())), after)
        self.assertTrue(verdict["render_task_woken"])

    def test_missing_snapshot_is_unavailable_not_a_refutation(self):
        verdict = rtos.assess_render_wake(
            {"available": False, "reason": "not requested"},
            rtos.snapshot(fake_qmp(build_image())))
        self.assertFalse(verdict["available"])
        self.assertFalse(verdict["render_task_woken"])


class LayoutCrossCheckTests(unittest.TestCase):
    def test_kernel_static_addresses_are_the_dwarf_declared_neighbors(self):
        """Adjacencies the factory DWARF actually shows, nothing inferred.

        If another build moves these, the decode must be re-derived instead of
        being silently trusted.
        """
        self.assertEqual(rtos.KERNEL["pxDelayedTaskList"] + 4,
                         rtos.KERNEL["xDelayedTaskList2"])
        self.assertEqual(rtos.KERNEL["xDelayedTaskList2"] + rtos.LIST_SIZE,
                         rtos.KERNEL["xDelayedTaskList1"])
        self.assertEqual(rtos.KERNEL["xDelayedTaskList1"] + rtos.LIST_SIZE,
                         rtos.KERNEL["pxReadyTasksLists"])
        self.assertEqual(rtos.KERNEL["pxReadyTasksLists"] +
                         rtos.MAX_PRIORITIES * rtos.LIST_SIZE,
                         rtos.KERNEL["pxCurrentTCBs"])

    def test_container_classification_covers_the_whole_ready_array(self):
        ram = rtos.GuestRam(fake_qmp(build_image()))
        last = (rtos.KERNEL["pxReadyTasksLists"] +
                (rtos.MAX_PRIORITIES - 1) * rtos.LIST_SIZE)
        self.assertEqual(rtos.container_kind(ram, last),
                         f"ready-priority-{rtos.MAX_PRIORITIES - 1}")
        self.assertEqual(rtos.container_kind(ram, last + 4), "other-list")
        self.assertEqual(rtos.container_kind(ram, 0), "unlinked")

    def test_recorded_blocked_container_is_the_queue_receive_list(self):
        """Observed 0x3fcec2b8 must be queue 0x3fcec294 + receive-list offset."""
        self.assertEqual(
            BLOCKED_QUEUE + rtos.QUEUE["x_tasks_waiting_to_receive"],
            0x3FCEC2B8)

    def test_factory_gate_constant_matches_recorded_runs(self):
        self.assertEqual(rtos.FACTORY_092C2BE5_FLASH_SHA256,
                         "206ed9661bd71477d1a54397c5a2cfe490179a3f59ab"
                         "094f1e7421745c609523")


def qmp_with_owner_hole(mem, item_address):
    """QMP stand-in that refuses only reads covering one list item's owner.

    `fake_qmp` maps the whole DRAM window, which is exactly the window
    `_plausible_pointer` accepts, so an unmodelled owner word reads back as 0
    and is classified `implausible-owner` instead. This wrapper makes one
    address genuinely unreadable, which is the only way to reach the
    `unreadable-owner` counter on the host.
    """
    inner = fake_qmp(mem)
    bad = item_address + rtos.LIST_ITEM["pv_owner"]

    def qmp(command, arguments):
        match = COMMAND.match(arguments["command-line"])
        start, count = int(match.group(2), 16), int(match.group(1))
        if start <= bad < start + 4 * count:
            return {"error": {"class": "GenericError",
                              "desc": "Invalid access"}}
        return inner(command, arguments)

    return qmp


class IndefiniteBlockDiscoveryTests(unittest.TestCase):
    """Gap 1: a portMAX_DELAY wait lives on xSuspendedTaskList.

    The shipped guest parks almost every application task there, so a walker
    that only visits the ready, delayed and pending-ready lists enumerates
    IDLE0/IDLE1/loopTask and reports the render task as unreachable.
    """

    def test_render_task_is_found_on_the_suspended_list(self):
        snap = rtos.snapshot(fake_qmp(build_image(
            render_state_list=SUSPENDED)))
        self.assertTrue(snap["available"], snap.get("read_errors"))
        found = {row["tcb"]: row["found_in"] for row in snap["tasks"]}
        self.assertEqual(found[hex(RENDER_TCB)], "suspended-list")
        self.assertEqual(sorted(snap["task_list_walk"]),
                         ["ready-priority-3", "suspended-list"])

    def test_suspended_block_reports_no_wake_tick_instead_of_a_number(self):
        """tasks.c:6381-6386: an infinite wait is not given a time to wake."""
        snap = rtos.snapshot(fake_qmp(build_image(
            render_state_list=SUSPENDED)))
        block = snap["render_task_block"]
        self.assertTrue(block["state_is_suspended"])
        self.assertFalse(block["state_is_delayed"])
        self.assertFalse(block["state_is_waiting_termination"])
        self.assertEqual(block["wake"], {
            "state_item_x_item_value": "0x5",
            "state_list": "suspended-list",
            "wake_tick_meaning": "no-wake-tick-indefinite-block-tasks-c-6381-6386",
            "wake_ticks_remaining": None,
            "x_tick_count": 0})

    def test_waiting_termination_is_a_separate_container(self):
        snap = rtos.snapshot(fake_qmp(build_image(
            render_state_list=TERMINATION)))
        self.assertTrue(snap["available"], snap.get("read_errors"))
        block = snap["render_task_block"]
        self.assertTrue(block["state_is_waiting_termination"])
        self.assertEqual(block["wake"]["state_list"], "waiting-termination")
        self.assertEqual(block["wake"]["wake_tick_meaning"],
                         "not-on-a-delayed-list-so-the-item-value-is-not-a-time")

    def test_a_task_that_is_neither_running_nor_listed_stays_unreachable(self):
        """The negative case: no fabricated render task from a broken image."""
        snap = rtos.snapshot(fake_qmp(build_image(
            render_state_list=0, render_event_list=0)))
        self.assertFalse(snap["available"])
        self.assertEqual(snap["reason"],
                         "render task not reachable from scheduler lists")
        self.assertIsNone(snap["render_task"])

    def test_initialised_empty_lists_are_quiet_in_the_walk_report(self):
        """Only lists with real content should surface as findings."""
        mem = build_image(initialise_extra=(SUSPENDED,))
        snap = rtos.snapshot(fake_qmp(mem))
        self.assertNotIn("suspended-list", snap["task_list_walk"])
        self.assertEqual(sorted(snap["task_list_walk"]),
                         ["delayed-list-1", "ready-priority-3"])
        ram = rtos.GuestRam(fake_qmp(mem))
        rows, report = rtos.walk_list(ram, SUSPENDED, set(), "suspended-list")
        self.assertEqual(rows, [])
        self.assertEqual(report, {"items_walked": 0, "duplicate-owner": 0,
                                  "implausible-owner": 0,
                                  "unreadable-owner": 0,
                                  "undecodable-task": 0})

    def test_zeroed_list_is_flagged_never_initialised_not_as_a_task(self):
        ram = rtos.GuestRam(fake_qmp(build_image(render_state_list=0,
                                                 render_event_list=0)))
        rows, report = rtos.walk_list(ram, SUSPENDED, set(), "suspended-list")
        self.assertEqual(rows, [])
        self.assertTrue(report["never-initialised"])


class TaskListWalkReportTests(unittest.TestCase):
    """Every way a list walk can fail is counted, never silently skipped."""

    def setUp(self):
        self.mem = build_image()
        relink(self.mem, SCRATCH_LIST, [])

    def walk(self, seen=None, ram=None):
        return rtos.walk_list(ram or rtos.GuestRam(fake_qmp(self.mem)),
                              SCRATCH_LIST, seen if seen is not None else set(),
                              "scratch")

    def test_duplicate_owner_is_counted_once_per_relisted_task(self):
        relink(self.mem, SCRATCH_LIST, [(FORGED_ITEM, RENDER_TCB)])
        self.assertEqual(self.walk(seen={RENDER_TCB})[1]["duplicate-owner"], 1)

    def test_owner_outside_dram_is_implausible_not_a_task_row(self):
        relink(self.mem, SCRATCH_LIST, [(FORGED_ITEM, 0x40000000)])
        rows, report = self.walk()
        self.assertEqual(rows, [])
        self.assertEqual(report["implausible-owner"], 1)

    def test_unreadable_owner_word_is_counted_and_the_error_kept(self):
        relink(self.mem, SCRATCH_LIST, [(FORGED_ITEM, RENDER_TCB)])
        ram = rtos.GuestRam(qmp_with_owner_hole(self.mem, FORGED_ITEM))
        rows, report = self.walk(ram=ram)
        self.assertEqual(rows, [])
        self.assertEqual(report["items_walked"], 1)
        self.assertEqual(report["unreadable-owner"], 1)
        self.assertTrue(ram.errors)
        self.assertEqual(ram.errors[0]["address"],
                         hex(FORGED_ITEM + rtos.LIST_ITEM["pv_owner"]))

    def test_tcb_without_a_readable_name_is_undecodable(self):
        relink(self.mem, SCRATCH_LIST, [(FORGED_ITEM, FORGED_TCB)])
        rows, report = self.walk()
        self.assertEqual(rows, [])
        self.assertEqual(report["undecodable-task"], 1)

    def test_item_address_outside_dram_stops_the_walk(self):
        relink(self.mem, SCRATCH_LIST, [(0x3FF00000, RENDER_TCB)])
        _rows, report = self.walk()
        self.assertEqual(report["implausible-item-address"], "0x3ff00000")

    def test_unreachable_guest_is_a_read_error_not_an_empty_list(self):
        _rows, report = rtos.walk_list(
            rtos.GuestRam(fake_qmp({}, holes=True)), SCRATCH_LIST, set(),
            "scratch")
        self.assertIs(report["read-error"], True)
        self.assertEqual(report["items_walked"], 0)

    def test_long_list_is_capped_and_says_so(self):
        relink(self.mem, SCRATCH_LIST,
               [(FORGED_ITEM + i * 0x20, FORGED_TCB)
                for i in range(rtos.MAX_LIST_ITEMS + 1)])
        rows, report = self.walk()
        self.assertEqual(rows, [])
        self.assertEqual(report["items_walked"], rtos.MAX_LIST_ITEMS)
        self.assertIs(report["capped-at-max-list-items"], True)
        self.assertEqual(report["duplicate-owner"], rtos.MAX_LIST_ITEMS - 1)
        self.assertEqual(report["undecodable-task"], 1)

    def test_enumeration_cap_is_reported_not_silent(self):
        mem = build_image()
        # 96 forged tasks, i.e. exactly MAX_ENUMERATED_TASKS, over the first
        # two ready priorities. The render task on delayed-list-1 and loopTask
        # on ready-priority-3 are then never reached, which is the point: the
        # cap has to be visible in the report instead of looking like an
        # absent application task.
        forged = [0x3FCEC000 + 0x1000 + index * rtos.TCB_SIZE
                  for index in range(rtos.MAX_ENUMERATED_TASKS)]
        for index, tcb in enumerate(forged):
            self.assertLess(tcb + rtos.TCB_SIZE, rtos.DRAM_HI,
                            f"fixture TCB {tcb:#x} runs past DRAM")
            forge_task(mem, tcb, f"T{index:02d}", 1,
                       READY0 if index < 64 else READY1, 0, 0, 0)
        for address, group in ((READY0, forged[:64]), (READY1, forged[64:])):
            relink(mem, address, [(tcb + rtos.TCB["x_state_list_item"], tcb)
                                  for tcb in group])
        ram = rtos.GuestRam(fake_qmp(mem))
        rows = rtos.enumerate_tasks(ram)
        self.assertEqual(len(rows), rtos.MAX_ENUMERATED_TASKS + 1)
        self.assertIs(ram.walk_report["capped-at-max-enumerated-tasks"], True)
        self.assertEqual(sorted(ram.walk_report),
                         ["capped-at-max-enumerated-tasks",
                          "ready-priority-0", "ready-priority-1"])
        snap = rtos.snapshot(fake_qmp(mem))
        self.assertEqual(snap["task_count"], rtos.MAX_ENUMERATED_TASKS + 1)
        self.assertFalse(snap["available"])


class WakeTickDecodeTests(unittest.TestCase):
    """Gap 2: the wake tick becomes a number only where one really exists.

    Each row is the complete decoder output for the named image, so a change
    in classification shows up as a diff rather than a passing test.
    """

    def decode(self, tick=1000, holes=False, **image):
        # With holes=True the fixture has to be empty: fake_qmp answers from
        # the map first and only errors on words it does not model.
        qmp = (fake_qmp({}, holes=True) if holes
               else fake_qmp(build_image(**image)))
        return rtos.decode_wake(rtos.GuestRam(qmp), RENDER_TCB, tick)

    def test_thirty_second_timeout_is_separated_from_the_twenty_ms_poll(self):
        self.assertEqual(self.decode(render_wake_tick=30000), {
            "candidate_branches": ["epd-busy-timeout-30s"],
            "distinguishable": True,
            "epd_wait_branch": "epd-busy-timeout-30s",
            "reason": "only-one-measured-window-is-long-enough",
            "remaining_ticks": 29000,
            "state_item_x_item_value": "0x7530",
            "state_list": "delayed-list-1",
            "wake_tick_meaning": "absolute-wake-tick-minus-xTickCount",
            "wake_ticks_remaining": 29000,
            "x_tick_count": 1000})

    def test_twenty_ms_timeout_alone_does_not_single_out_a_branch(self):
        """20 ticks remaining fits both windows, so the answer stays open."""
        self.assertEqual(self.decode(render_wake_tick=1020), {
            "candidate_branches": ["epd-busy-poll-20ms",
                                   "epd-busy-timeout-30s"],
            "distinguishable": False,
            "epd_wait_branch": None,
            "reason": ("both-windows-fit-a-short-remaining-time; a sample "
                       "taken right after the wait starts separates them"),
            "remaining_ticks": 20,
            "state_item_x_item_value": "0x3fc",
            "state_list": "delayed-list-1",
            "wake_tick_meaning": "absolute-wake-tick-minus-xTickCount",
            "wake_ticks_remaining": 20,
            "x_tick_count": 1000})

    def test_remaining_time_above_both_windows_is_not_epd_busy_wait(self):
        self.assertEqual(self.decode(render_wake_tick=31001)["reason"],
                         "remaining-exceeds-both-measured-windows")

    def test_wake_time_exactly_reached_is_reported_as_zero(self):
        decoded = self.decode(render_wake_tick=1000)
        self.assertEqual(decoded["wake_ticks_remaining"], 0)
        self.assertEqual(decoded["reason"], "wake-time-already-reached")

    def test_elapsed_wake_tick_is_not_wrapped_into_a_huge_remaining_time(self):
        """0x384 - 1000 is negative; reporting 4294967196 would be a lie."""
        self.assertEqual(self.decode(render_wake_tick=900), {
            "state_item_x_item_value": "0x384",
            "state_list": "delayed-list-1",
            "unsigned_difference": "0xffffff9c",
            "wake_tick_meaning": "wake-tick-already-elapsed-pending-tick-interrupt",
            "wake_ticks_remaining": None,
            "x_tick_count": 1000})

    def test_the_second_delayed_list_is_a_delayed_list_too(self):
        decoded = self.decode(render_state_list=DELAYED2,
                              render_wake_tick=30000)
        self.assertEqual(decoded["state_list"], "delayed-list-2")
        self.assertEqual(decoded["wake_ticks_remaining"], 29000)

    def test_ready_task_stale_item_value_is_not_read_as_a_time(self):
        self.assertEqual(
            self.decode(render_state_list=READY3,
                        render_wake_tick=30000)["wake_tick_meaning"],
            "not-on-a-delayed-list-so-the-item-value-is-not-a-time")

    def test_suspended_task_stale_item_value_is_not_read_as_a_time(self):
        decoded = self.decode(render_state_list=SUSPENDED,
                              render_wake_tick=30000)
        self.assertEqual(decoded["wake_tick_meaning"],
                         "no-wake-tick-indefinite-block-tasks-c-6381-6386")
        self.assertIsNone(decoded["wake_ticks_remaining"])
        # The kernel leaves the old value behind; the decoder reports it as
        # data, but never turns it into a remaining time.
        self.assertEqual(decoded["state_item_x_item_value"], "0x7530")

    def test_unreadable_state_item_yields_no_tick(self):
        self.assertEqual(self.decode(holes=True), {
            "state_item_x_item_value": None,
            "state_list": None,
            "wake_tick_meaning": "unreadable",
            "wake_ticks_remaining": None,
            "x_tick_count": 1000})

    def test_missing_tick_count_is_read_from_the_guest(self):
        mem = build_image(render_wake_tick=30000, tick_count=1000)
        self.assertEqual(
            rtos.decode_wake(rtos.GuestRam(fake_qmp(mem)), RENDER_TCB),
            rtos.decode_wake(rtos.GuestRam(fake_qmp(mem)), RENDER_TCB, 1000))

    def test_a_task_that_leaves_the_suspended_list_picks_up_a_wake_tick(self):
        before = rtos.snapshot(fake_qmp(build_image(
            render_state_list=SUSPENDED)))
        after = rtos.snapshot(fake_qmp(build_image(
            render_wake_tick=30000, tick_count=1000)))
        pair = rtos.assess_render_wake(before, after)
        self.assertEqual(pair["reason"], "ok")
        self.assertTrue(pair["left_indefinite_block"])
        self.assertIsNone(pair["wake_ticks_remaining_before"])
        self.assertEqual(pair["wake_ticks_remaining_after"], 29000)
        self.assertEqual(pair["epd_wait_branch_after"], "epd-busy-timeout-30s")
        self.assertTrue(pair["still_parked_in_delayed_list"])
        # Honest: a delayed task is armed to run, not running.
        self.assertFalse(pair["render_task_woken"])


class EpdWaitClassificationTests(unittest.TestCase):
    """classify_epd_wait only names a branch when the number allows it."""

    EXPECTED = [
        (None, [], None, False, "no-wake-tick-measured"),
        (0, [], None, False, "wake-time-already-reached"),
        (-5, [], None, False, "wake-time-already-reached"),
        (20, ["epd-busy-poll-20ms", "epd-busy-timeout-30s"], None, False,
         "both-windows-fit-a-short-remaining-time; a sample taken right "
         "after the wait starts separates them"),
        (21, ["epd-busy-timeout-30s"], "epd-busy-timeout-30s", True,
         "only-one-measured-window-is-long-enough"),
        (30000, ["epd-busy-timeout-30s"], "epd-busy-timeout-30s", True,
         "only-one-measured-window-is-long-enough"),
        (30001, [], None, False, "remaining-exceeds-both-measured-windows"),
    ]

    def test_classification_table(self):
        for remaining, candidates, branch, distinct, reason in self.EXPECTED:
            with self.subTest(remaining=remaining):
                self.assertEqual(rtos.classify_epd_wait(remaining), {
                    "candidate_branches": candidates,
                    "epd_wait_branch": branch,
                    "distinguishable": distinct,
                    "reason": reason,
                    "remaining_ticks": remaining})

    def test_branches_cite_the_source_lines_they_came_from(self):
        self.assertEqual({(b["label"], b["ticks"], b["source"])
                          for b in rtos.EPD_WAIT_BRANCHES},
                         {("epd-busy-poll-20ms", 20, "EpdBus.cpp:445"),
                          ("epd-busy-timeout-30s", 30000, "EpdBus.cpp:460")})


# Kernel statics for the synthetic ELF below. They are deliberately NOT
# rtos.KERNEL: a fixture that reproduced the recorded table would pass even if
# from_elf ignored the file and returned the constants, which is the exact
# failure mode this lane is not allowed to have.
LAYOUT_SYMBOLS = {
    "xSchedulerRunning": (0x3FCABF90, 4),
    "xTickCount": (0x3FCABF94, 4),
    "xNextTaskUnblockTime": (0x3FCABFA8, 4),
    "pxDelayedTaskList": (0x3FCABFF8, 4),
    "xPendingReadyList": (0x3FCABFC0, 2 * rtos.LIST_SIZE),
    "xSuspendedTaskList": (0x3FCABF9C, rtos.LIST_SIZE),
    "xTasksWaitingTermination": (0x3FCABFB4, rtos.LIST_SIZE),
    "xDelayedTaskList1": (0x3FCAC00C, rtos.LIST_SIZE),
    "xDelayedTaskList2": (0x3FCABFFC, rtos.LIST_SIZE),
    "pxReadyTasksLists": (0x3FCAC034, rtos.MAX_PRIORITIES * rtos.LIST_SIZE),
    "pxCurrentTCBs": (0x3FCAC418, 8),
}


def elf_bytes(entries, class_byte=1, big_endian=False, shstrndx=3, entsize=40,
              types=None):
    """A minimal 32-bit little-endian ELF holding `.symtab` and `.strtab`.

    `entries` is a name -> (address, size) map, or a list of
    (name, address, size) triples when one name has to appear twice. Only the
    fields `read_elf_object_symbols` reads are laid out faithfully; the rest of
    an ELF is not needed and is not invented here.
    """
    records = ([(name, value, size)
                for name, (value, size) in sorted(entries.items())]
               if isinstance(entries, dict) else sorted(entries))
    strtab = b"\0"
    offsets = []
    for name, _value, _size in records:
        offsets.append(len(strtab))
        strtab += name.encode() + b"\0"
    syms = b"\0" * 16  # the index-0 null symbol
    for (name, value, size), name_offset in zip(records, offsets):
        # st_info packs the type in the LOW nibble, so 1 is STT_OBJECT.
        syms += struct.pack("<IIIBBH", name_offset, value, size,
                            (types or {}).get(name, rtos._ELF_STT_OBJECT), 0, 1)
    names = ["", ".symtab", ".strtab", ".shstrtab"]
    shstrtab = b"\0" + b"\0".join(name.encode() for name in names[1:]) + b"\0"
    body = strtab + syms + shstrtab
    shoff = 52 + len(body)

    def header(name_index, sh_type, offset, size, link=0, sh_entsize=0):
        return struct.pack("<10I", name_index, sh_type, 0, 0, offset, size,
                           link, 0, 4, sh_entsize)

    sections = [
        header(0, 0, 0, 0, 0, 0),                                    # SHT_NULL
        header(1, rtos._ELF_SHT_SYMTAB, 52 + len(strtab), len(syms), 2, 16),
        header(2, 3, 52, len(strtab), 0, 0),                         # SHT_STRTAB
        header(3, 3, 52 + len(strtab) + len(syms), len(shstrtab), 0, 0),
    ]
    ident = (b"\x7fELF" + bytes([class_byte, 2 if big_endian else 1, 1, 0, 0])
             + b"\0" * 7)
    return (ident + struct.pack("<HHIIIIIHHHHHH", 2, 229, 1, 0, 0, shoff, 0,
                                52, 0, 0, entsize, len(sections), shstrndx)
            + body + b"".join(sections))


def patch_elf_word(payload, offset, value):
    """Rewrite one 32-bit word of a synthetic ELF, for corrupt-header tests."""
    mutated = bytearray(payload)
    struct.pack_into("<I", mutated, offset, value)
    return bytes(mutated)


def symtab_word(payload, field):
    """Byte offset of one Elf32_Shdr word of the `.symtab` entry (index 1)."""
    shoff, = struct.unpack_from("<I", payload, 0x20)
    return shoff + 40 + 4 * field


class ElfLayoutDiscoveryTests(unittest.TestCase):
    """`KernelLayout.from_elf` reads a layout; every other path refuses."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = Path(holder.name)

    def write(self, payload, name="firmware.elf"):
        path = self.dir / name
        path.write_bytes(payload)
        return path

    def refuse(self, payload, needle, name="bad.elf"):
        path = self.write(payload, name)
        with self.assertRaises(rtos.LayoutError) as caught:
            rtos.KernelLayout.from_elf(path)
        self.assertIn(needle, str(caught.exception))
        return str(caught.exception)

    def test_addresses_and_bounds_come_from_the_symbol_table(self):
        layout = rtos.KernelLayout.from_elf(self.write(elf_bytes(LAYOUT_SYMBOLS)))
        self.assertEqual(layout.addresses,
                         {name: value
                          for name, (value, _size) in LAYOUT_SYMBOLS.items()})
        self.assertNotEqual(layout.addresses, dict(rtos.KERNEL))
        # Array sizes, not literals: 500 bytes is 25 List_t, 40 bytes is 2.
        self.assertEqual(layout.max_priorities, rtos.MAX_PRIORITIES)
        self.assertEqual(layout.cores, rtos.CORES)

    def test_recorded_epd_handle_is_kept_when_the_symbol_is_absent(self):
        layout = rtos.KernelLayout.from_elf(self.write(elf_bytes(LAYOUT_SYMBOLS)))
        self.assertEqual(layout.epd_refresh_done, rtos.EPD_REFRESH_DONE_HANDLE)
        self.assertTrue(layout.source.startswith("discovered from "))
        self.assertIn("not in symtab, EPD handle stays recorded",
                      layout.source)

    def test_epd_semaphore_address_is_discovered_too(self):
        entries = dict(LAYOUT_SYMBOLS,
                       **{rtos.EPD_SEMAPHORE_SYMBOL: (0x3FCA1F00, 4)})
        layout = rtos.KernelLayout.from_elf(self.write(elf_bytes(entries)))
        self.assertEqual(layout.epd_refresh_done, 0x3FCA1F00)
        self.assertNotIn("stays recorded", layout.source)

    def test_a_symbol_the_decoder_needs_must_be_present(self):
        for key in rtos.LIST_KEYS + rtos.WORD_KEYS + ("pxCurrentTCBs",):
            with self.subTest(key=key):
                entries = {name: value
                           for name, value in LAYOUT_SYMBOLS.items()
                           if name != key}
                self.refuse(elf_bytes(entries), f"no {key} symbol")

    def test_function_symbols_are_not_object_symbols(self):
        self.refuse(elf_bytes(LAYOUT_SYMBOLS, types={"xTickCount": 2}),
                    "no xTickCount symbol")

    def test_one_name_two_addresses_is_ambiguous_not_guessed(self):
        entries = [(name, value, size)
                   for name, (value, size) in LAYOUT_SYMBOLS.items()]
        entries.append(("xTickCount", 0x3FCABF00, 4))
        message = self.refuse(elf_bytes(entries), "xTickCount is ambiguous")
        self.assertIn("0x3fcabf00", message)
        self.assertIn("0x3fcabf94", message)

    def test_symbol_sizes_that_do_not_match_the_kernel_are_refused(self):
        def with_(**changed):
            entries = dict(LAYOUT_SYMBOLS)
            entries.update(changed)
            return elf_bytes(entries)

        cases = [
            (dict(xTickCount=(0x3FCABF94, 8)), "xTickCount is 8 bytes, not one word"),
            (dict(xDelayedTaskList1=(0x3FCAC00C, 21)),
             "xDelayedTaskList1 is 21 bytes, not a whole number of "
             "20-byte List_t"),
            (dict(xDelayedTaskList1=(0x3FCAC00C, 0)),
             "xDelayedTaskList1 is 0 bytes, not a whole number of "
             "20-byte List_t"),
            (dict(pxCurrentTCBs=(0x3FCAC418, 7)),
             "pxCurrentTCBs is 7 bytes, not an array of pointers"),
            (dict(pxCurrentTCBs=(0x3FCAC418, 12)),
             "pxCurrentTCBs holds 3 pointers but xPendingReadyList implies "
             "2 cores"),
        ]
        for changed, needle in cases:
            with self.subTest(**changed):
                self.refuse(with_(**changed), needle)

    def test_derived_bounds_are_checked_against_the_kernel(self):
        entries = dict(LAYOUT_SYMBOLS,
                       **{"pxReadyTasksLists": (0x3FCAC034,
                                                257 * rtos.LIST_SIZE),
                          "xPendingReadyList": (0x3FCABFC0, rtos.LIST_SIZE),
                          "pxCurrentTCBs": (0x3FCAC418, 4)})
        self.refuse(elf_bytes(entries),
                    "derived bounds are implausible (max_priorities=257, cores=1)")

    def test_addresses_outside_dram_are_refused(self):
        cases = [("xTickCount", 0x3FCABF95, 4),
                 ("xTickCount", 0x60000000, 4),
                 ("pxReadyTasksLists", 0x3FC80000,
                  rtos.MAX_PRIORITIES * rtos.LIST_SIZE)]
        for name, value, size in cases:
            with self.subTest(name=name, value=hex(value)):
                entries = dict(LAYOUT_SYMBOLS, **{name: (value, size)})
                self.refuse(elf_bytes(entries),
                            f"{name} at 0x{value:08x} is not a word-aligned "
                            f"DRAM address")

    def test_malformed_elf_containers_are_refused(self):
        payload = elf_bytes(LAYOUT_SYMBOLS)
        cases = [
            (patch_elf_word(payload, 4, 2), "only 32-bit ELFs are supported"),
            (patch_elf_word(payload, 5, 2), "big-endian ELFs are not supported"),
            (payload[:100], "section header table runs past the file"),
            (b"not an elf at all", "not an ELF file"),
            (elf_bytes(LAYOUT_SYMBOLS, shstrndx=9), "e_shstrndx 9 is out of range"),
            (elf_bytes(LAYOUT_SYMBOLS, entsize=20),
             "section header table runs past the file"),
            (patch_elf_word(payload, symtab_word(payload, 5), 1 << 30),
             ".symtab (index 1) runs past the file"),
            (patch_elf_word(payload, symtab_word(payload, 6), 99),
             "symtab 1 points outside the file"),
        ]
        for mutated, needle in cases:
            with self.subTest(needle=needle):
                self.refuse(mutated, needle)

    def test_missing_file_is_a_layout_error_not_a_traceback(self):
        with self.assertRaises(rtos.LayoutError) as caught:
            rtos.KernelLayout.from_elf(self.dir / "absent.elf")
        self.assertIn("No such file or directory", str(caught.exception))


FACTORY_ELF = Path(os.environ.get(
    "X4PRO_FIRMWARE_ELF",
    "/Users/yaraslauhancharou/.local/state/xteink-agent-workspaces/"
    "fw-092c2be5-symbols-20261029/.pio/build/x4pro/firmware.elf"))


class FactoryElfCrossCheckTests(unittest.TestCase):
    """The real factory ELF must reproduce the recorded table, or the recorded
    table is a guess. Skipped when the read-only symbol bundle is not present."""

    @unittest.skipUnless(FACTORY_ELF.is_file(), "factory firmware ELF missing")
    def test_factory_elf_yields_the_recorded_kernel_layout(self):
        layout = rtos.KernelLayout.from_elf(FACTORY_ELF)
        self.assertEqual(layout.addresses, dict(rtos.KERNEL))
        self.assertEqual(layout.max_priorities, rtos.MAX_PRIORITIES)
        self.assertEqual(layout.cores, rtos.CORES)
        self.assertEqual(layout.epd_refresh_done, rtos.EPD_REFRESH_DONE_HANDLE)
        self.assertNotIn("stays recorded", layout.source)
        # Discovered offsets decode the fixture whose addresses are the
        # recorded ones, so a live snapshot needs no hardcoded table at all.
        snap = rtos.snapshot(fake_qmp(build_image()), layout=layout)
        self.assertTrue(snap["available"], snap.get("reason"))


class WrongBuildRefusalTests(unittest.TestCase):
    """A guest that does not match the layout must report, never guess."""

    def test_shifted_layout_finds_no_render_task_and_says_so(self):
        shifted = rtos.KernelLayout(
            {key: value + 0x40 for key, value in rtos.KERNEL.items()},
            rtos.MAX_PRIORITIES, rtos.CORES,
            rtos.EPD_REFRESH_DONE_HANDLE + 0x40, "synthetic shifted build")
        snap = rtos.snapshot(fake_qmp(build_image()), layout=shifted)
        self.assertFalse(snap["available"])
        self.assertEqual(snap["reason"],
                         "render task not reachable from scheduler lists")
        self.assertIsNone(snap["render_task"])
        # The only task row is the running one read from shifted RAM; the walk
        # reports implausible owners instead of inventing names.
        self.assertEqual(snap["task_count"], 1)
        self.assertNotIn("read_errors", snap)

    def test_unreadable_guest_ram_is_reported_as_unmatched_build(self):
        shifted = rtos.KernelLayout(
            {key: value + 0x40 for key, value in rtos.KERNEL.items()},
            rtos.MAX_PRIORITIES, rtos.CORES,
            rtos.EPD_REFRESH_DONE_HANDLE + 0x40, "synthetic shifted build")
        snap = rtos.snapshot(fake_qmp({}, holes=True), layout=shifted)
        self.assertFalse(snap["available"])
        self.assertEqual(snap["reason"],
                         "guest RAM read failed; wrong or unmatched build")


def probe_cli(argv):
    """Run the wake probe's CLI with a mock monitor that must not be reached.

    Returns (exit code, stdout, stderr) and the connection mock, so a test can
    prove the SHA and ELF gates refuse before anything attaches to a guest.
    """
    connect = mock.MagicMock(
        side_effect=AssertionError("must not attach to a guest"))
    stdout, stderr = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", ["probe.py"] + list(argv)), \
            mock.patch.object(wake_probe, "qmp_connect", connect), \
            contextlib.redirect_stdout(stdout), \
            contextlib.redirect_stderr(stderr):
        code = wake_probe.main()
    return code, stdout.getvalue(), stderr.getvalue(), connect


class WakeProbeTests(unittest.TestCase):
    """The delivered probe's own helpers, exercised without a guest."""

    @staticmethod
    def sample(phase, available=True, wake=None, state_list=None):
        block = {}
        if wake is not None or state_list is not None:
            block["wake"] = dict(wake or {})
            if state_list is not None:
                block["wake"]["state_list"] = state_list
        return {"phase": phase,
                "wall": 0.0,
                "snapshot": {"available": available,
                             "x_tick_count": 1000,
                             "render_task_block": block}}

    def test_contacts_are_the_verified_raw_gt911_coordinates(self):
        self.assertEqual(wake_probe.CONTACTS, {
            "more": (350, 760), "ybx-settings": (240, 453),
            "system-settings": (210, 679), "back": (35, 61)})

    def test_sensor_writes_the_documented_qom_properties(self):
        seen = []

        def execute(command, arguments):
            seen.append((command, arguments))
            return {"return": {}}

        self.assertEqual(wake_probe.sensor(execute, "x", 350),
                         {"ok": True, "reply": {"return": {}}})
        self.assertEqual(seen, [("qom-set", {"path": "/machine/soc",
                                             "property": "ybx-touch-x",
                                             "value": 350})])
        refused = wake_probe.sensor(
            lambda command, arguments: {"error": {"class": "GenericError"}},
            "state", 1)
        self.assertFalse(refused["ok"])

    def test_wake_of_never_raises_on_partial_snapshots(self):
        self.assertEqual(wake_probe.wake_of(None), {})
        self.assertEqual(wake_probe.wake_of({"render_task_block": {}}), {})
        self.assertEqual(wake_probe.wake_of(
            {"render_task_block": {"wake": None}}), {})
        self.assertEqual(wake_probe.wake_of(
            {"render_task_block": {"wake": {"wake_ticks_remaining": 29500}}}),
            {"wake_ticks_remaining": 29500})

    def test_tick_rate_divides_ticks_by_observed_wall_time(self):
        measured = wake_probe.tick_rate([
            {"wall": 10.0, "snapshot": {"x_tick_count": 1000}},
            {"wall": 10.5, "snapshot": {"x_tick_count": 1500}}])
        self.assertEqual(measured["points"], 2)
        self.assertEqual(measured["measured"], {"ticks": 500, "seconds": 0.5,
                                               "ticks_per_second": 1000.0})
        self.assertEqual(measured["assumed_hz"], 1000)
        self.assertIn("pdMS_TO_TICKS", measured["note"])

    def test_tick_rate_wraps_at_the_counter_width(self):
        rate = wake_probe.tick_rate([
            {"wall": 0.0, "snapshot": {"x_tick_count": 0xFFFFFFF8}},
            {"wall": 1.0, "snapshot": {"x_tick_count": 8}}])["measured"]
        self.assertEqual(rate["ticks"], 16)

    def test_tick_rate_reports_nothing_when_the_guest_is_unreadable(self):
        report = wake_probe.tick_rate([
            {"wall": 0.0, "snapshot": {"x_tick_count": None}},
            {"wall": 1.0, "snapshot": {"x_tick_count": None}}])
        self.assertEqual(report["points"], 0)
        self.assertIsNone(report["measured"])

    def test_verdict_answers_the_branch_only_when_a_tick_separates_them(self):
        long_window = self.sample("after-1", wake={
            "wake_ticks_remaining": 29500, "distinguishable": True,
            "epd_wait_branch": "epd-busy-timeout-30s", "candidate_branches":
            ["epd-busy-timeout-30s"],
            "reason": "only-one-measured-window-is-long-enough"})
        out = wake_probe.verdict([self.sample("before"), long_window], {})
        self.assertEqual(out["answer"], "epd-busy-timeout-30s")
        self.assertEqual(out["wake_ticks_remaining_after_max"], 29500)
        self.assertEqual(out["epd_wait_candidates"], ["epd-busy-timeout-30s"])
        self.assertTrue(out["render_task_reachable"])

    def test_verdict_says_ambiguous_when_both_windows_still_fit(self):
        short = self.sample("after-1", wake={
            "wake_ticks_remaining": 20, "distinguishable": False,
            "epd_wait_branch": None, "candidate_branches":
            ["epd-busy-poll-20ms", "epd-busy-timeout-30s"],
            "reason": "both-windows-fit-a-short-remaining-time"})
        out = wake_probe.verdict([short], {})
        self.assertEqual(out["answer"], "ambiguous")
        self.assertIn("sample earlier or faster", out["reason"])
        self.assertEqual(out["wake_ticks_remaining_after_max"], 20)

    def test_verdict_reports_a_task_that_never_entered_a_timed_wait(self):
        parked = self.sample("after-1", state_list="suspended-list",
                             wake={"wake_ticks_remaining": None,
                                   "wake_tick_meaning":
                                   "no-wake-tick-indefinite-block-"
                                   "tasks-c-6381-6386"})
        out = wake_probe.verdict([self.sample("before"), parked], {})
        self.assertEqual(out["answer"], "not-measured")
        self.assertEqual(out["post_contact_states"], ["suspended-list"])
        self.assertEqual(out["wake_tick_meanings"],
                         ["no-wake-tick-indefinite-block-tasks-c-6381-6386"])
        self.assertIn("no wake tick exists to classify", out["reason"])

    def test_verdict_says_not_measured_when_no_after_sample_decodes(self):
        out = wake_probe.verdict([self.sample("before"),
                                  self.sample("after-1", available=False)], {})
        self.assertEqual(out["answer"], "not-measured")
        self.assertTrue(out["render_task_reachable"])
        self.assertIn("not reachable in any post-contact sample", out["reason"])


class WakeProbeGuestSimulationTests(unittest.TestCase):
    """`main()` end-to-end against a simulated guest, since no real one runs here."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = Path(holder.name)

    def guest(self, before, after):
        """A QMP `execute` that swaps images when the finger lifts."""
        state = {"mem": before, "sets": []}

        def execute(command, arguments):
            if command == "human-monitor-command":
                return fake_qmp(state["mem"])(command, arguments)
            if command == "qom-set":
                state["sets"].append((arguments["property"],
                                      arguments["value"]))
                if (arguments["property"] == "ybx-touch-state"
                        and arguments["value"] == 0):
                    state["mem"] = after
                return {"return": {}}
            raise AssertionError(f"unexpected QMP command {command}")
        return execute, state

    def run_probe(self, argv, execute):
        client = mock.MagicMock()
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["probe.py"] + list(argv)), \
                mock.patch.object(wake_probe, "qmp_connect",
                                  return_value=(client, None, execute)), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            code = wake_probe.main()
        self.assertTrue(client.close.called, "main() must release the monitor")
        return code, json.loads(stdout.getvalue()), stdout.getvalue(), stderr.getvalue()

    def test_main_measures_the_thirty_second_window_after_one_contact(self):
        execute, state = self.guest(build_image(render_state_list=SUSPENDED),
                                    build_image(render_wake_tick=30500,
                                                tick_count=1000))
        out_path = self.dir / "wake.json"
        code, report, printed, errors = self.run_probe(
            ["--qmp", str(self.dir / "qmp.sock"), "--flash-sha256",
             rtos.FACTORY_092C2BE5_FLASH_SHA256, "--contact", "more",
             "--samples", "2", "--sample-interval", "0",
             "--press-seconds", "0", "--out", str(out_path)], execute)
        self.assertEqual(code, 0, errors)
        self.assertEqual(report["verdict"]["answer"], "epd-busy-timeout-30s")
        self.assertEqual(report["verdict"]["wake_ticks_remaining_after_max"],
                         29500)
        pair = report["render_task_woken_after_contact"]
        self.assertTrue(pair["left_indefinite_block"])
        self.assertEqual(pair["wake_ticks_remaining_before"], None)
        self.assertEqual(pair["wake_ticks_remaining_after"], 29500)
        self.assertEqual(pair["epd_wait_branch_after"], "epd-busy-timeout-30s")
        self.assertTrue(pair["still_parked_in_delayed_list"])
        self.assertEqual(report["contact"]["raw_gt911_xy"], [350, 760])
        self.assertEqual(state["sets"], [("ybx-touch-x", 350),
                                         ("ybx-touch-y", 760),
                                         ("ybx-touch-state", 1),
                                         ("ybx-touch-state", 0)])
        self.assertEqual(set(report["injection_results"]),
                         {"set-x", "set-y", "press", "release"})
        self.assertTrue(all(result["ok"] for result in
                            report["injection_results"].values()))
        self.assertEqual([s["phase"] for s in report["samples"]],
                         ["before", "after-1", "after-2"])
        # Gap 1: the before-snapshot reaches the render task even though it is
        # parked on xSuspendedTaskList, which the old walker never visited.
        # configMAX_TASK_NAME_LEN is 16, so pcTaskName stores the truncation.
        self.assertTrue(report["samples"][0]["available"])
        self.assertEqual(report["samples"][0]["render_task"]["name"],
                         "ActivityManagerR")
        self.assertIsNone(report["samples"][0]["render_task_block"]["wake"][
            "wake_ticks_remaining"])
        self.assertIn("decoded with layout", report["samples"][0]["reason"])
        self.assertEqual(report["layout_source"],
                         "recorded factory 092c2be5 statics (no --elf given)")
        self.assertIn("neither proves nor denies",
                      report["not_a_framebuffer_claim"])
        self.assertEqual(out_path.read_text(), printed,
                         "--out must hold exactly what was printed")

    def test_skip_contact_leaves_the_wake_question_unanswered(self):
        execute, state = self.guest(build_image(render_state_list=SUSPENDED),
                                    build_image(render_wake_tick=30500,
                                                tick_count=1000))
        code, report, printed, errors = self.run_probe(
            ["--qmp", str(self.dir / "qmp.sock"), "--flash-sha256",
             rtos.FACTORY_092C2BE5_FLASH_SHA256, "--skip-contact",
             "--samples", "1", "--sample-interval", "0",
             "--press-seconds", "0"], execute)
        self.assertEqual(code, 0, errors)
        self.assertEqual(state["sets"], [])
        self.assertEqual(report["injection_results"], {})
        self.assertTrue(report["contact"]["skipped"])
        self.assertEqual(report["verdict"]["answer"], "not-measured")
        self.assertIn("no wake tick exists to classify",
                      report["verdict"]["reason"])
        self.assertTrue(report["verdict"]["render_task_reachable"])


class WakeProbeCliGateTests(unittest.TestCase):
    """The refusals that keep the probe from decoding a foreign build."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = Path(holder.name)

    def test_a_non_factory_flash_is_refused_before_anything_is_decoded(self):
        code, printed, errors, connect = probe_cli(
            ["--qmp", str(self.dir / "qmp.sock"), "--flash-sha256", "0" * 64])
        self.assertEqual(code, 2)
        self.assertEqual(printed, "")
        self.assertIn("Refusing to decode", errors)
        self.assertIn(rtos.FACTORY_092C2BE5_FLASH_SHA256, errors)
        self.assertFalse(connect.called,
                         "the SHA gate must run before any connection")

    def test_the_flash_file_is_hashed_not_trusted(self):
        flash = self.dir / "other.bin"
        flash.write_bytes(b"not the factory image")
        code, printed, errors, connect = probe_cli(
            ["--qmp", str(self.dir / "qmp.sock"), "--flash", str(flash)])
        self.assertEqual(code, 2)
        self.assertIn("Refusing to decode", errors)
        self.assertIn(hashlib.sha256(flash.read_bytes()).hexdigest(), errors)
        self.assertFalse(connect.called,
                         "the SHA gate must run before any connection")

    def test_an_elf_that_is_not_this_build_fails_layout_discovery(self):
        code, printed, errors, connect = probe_cli(
            ["--qmp", str(self.dir / "qmp.sock"), "--flash-sha256",
             rtos.FACTORY_092C2BE5_FLASH_SHA256, "--elf",
             str(self.dir / "absent.elf")])
        self.assertEqual(code, 2)
        self.assertIn("ELF layout discovery failed", errors)
        self.assertFalse(
            connect.called, "a bad layout must not reach a live guest")

    def test_tcp_monitor_needs_both_host_and_port(self):
        with self.assertRaises(SystemExit) as caught:
            probe_cli(["--qmp-host", "127.0.0.1"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
