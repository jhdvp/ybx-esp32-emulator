"""Host tests for the read-only FreeRTOS guest-RAM decoder.

The synthetic image uses the addresses recorded in the real factory guest runs
(render TCB 0x3fcec420, blocked queue 0x3fcec294, xDelayedTaskList1 0x3fcac00c)
so the layout, not just the arithmetic, is pinned to evidence.
"""
import re
import unittest

import freertos_guest_probe as rtos

RENDER_TCB = 0x3FCEC420
WORKER_TCB = 0x3FCEC900
BLOCKED_QUEUE = 0x3FCEC294
DELAYED1 = rtos.KERNEL["xDelayedTaskList1"]
READY3 = rtos.KERNEL["pxReadyTasksLists"] + 3 * rtos.LIST_SIZE


def build_image(notify_value=1, notify_state=2, render_state_list=DELAYED1,
                render_event_list=BLOCKED_QUEUE +
                rtos.QUEUE["x_tasks_waiting_to_receive"],
                epd_handle=BLOCKED_QUEUE, current_tcb=WORKER_TCB,
                busy_input=0x609B, scheduler=1):
    """Little-endian word map of a minimal, self-consistent FreeRTOS guest."""
    mem = {}

    def put(address, *values):
        for offset, value in enumerate(values):
            mem[address + 4 * offset] = value & 0xFFFFFFFF

    def task(tcb, name, priority, state_list, event_list, value, state):
        raw = name.encode().ljust(16, b"\0")[:16]
        put(tcb + rtos.TCB["ux_priority"], priority)
        put(tcb + rtos.TCB["ux_base_priority"], priority)
        put(tcb + rtos.TCB["ux_mutexes_held"], 1)
        put(tcb + rtos.TCB["ux_tcb_number"], 7)
        put(tcb + rtos.TCB["x_core_id"], 1)
        put(tcb + rtos.TCB["ul_run_time_counter"], 1234)
        put(tcb + rtos.TCB["ul_notified_value"], value)
        put(tcb + rtos.TCB["uc_notify_state"], state)
        for offset in range(0, 16, 4):
            put(tcb + rtos.TCB["pc_task_name"] + offset,
                int.from_bytes(raw[offset:offset + 4], "little"))
        state_item = tcb + rtos.TCB["x_state_list_item"]
        event_item = tcb + rtos.TCB["x_event_list_item"]
        for item, container in ((state_item, state_list),
                                (event_item, event_list)):
            put(item + rtos.LIST_ITEM["x_item_value"], 5)
            put(item + rtos.LIST_ITEM["px_next"], item)
            put(item + rtos.LIST_ITEM["px_previous"], item)
            put(item + rtos.LIST_ITEM["pv_owner"], tcb)
            put(item + rtos.LIST_ITEM["pv_container"], container)

    task(RENDER_TCB, "ActivityManagerRender", 3, render_state_list,
         render_event_list, notify_value, notify_state)
    task(WORKER_TCB, "LoopTask", 3, READY3, 0, 0, 0)

    # Kernel statics: list membership follows the task containers above, so
    # the image is self-consistent the way the kernel leaves it.
    put(rtos.KERNEL["xSchedulerRunning"], scheduler)
    put(rtos.KERNEL["pxCurrentTCBs"], current_tcb, current_tcb)
    put(rtos.KERNEL["pxDelayedTaskList"], DELAYED1)
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
              for priority in range(rtos.MAX_PRIORITIES)])
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


if __name__ == "__main__":
    unittest.main()
