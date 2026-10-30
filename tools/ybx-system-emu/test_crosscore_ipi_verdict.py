"""Verdict classification for the X4 Pro cross-core IPI vs unrelated-wait question."""
import unittest
from crosscore_ipi_verdict import classify, parse_probe_log

DELAYED = 0x3FCAC00C
READY = 0x3FCAC070
PENDING0 = 0x3FCABFCC
PENDING1 = 0x3FCABFDC
QUEUE = 0x3FCEC294
WAIT_LIST = QUEUE + 40  # xTasksWaitingToReceive offset inside Queue_t


def log(ipi_window=0, ipi_total_extra=0, taken_isr=0, sem_give=0, sem_give_obj=0x3FCEC294,
        state=DELAYED, event=WAIT_LIST, notify_state=0, notified=1,
        sem_type=3, messages=0, waiters=1, core=1, prio=4,
        pend_items=0, suspended=0):
    lines = ["NOTIFY_PRE handle=0x3fcec420"]
    for _ in range(ipi_window):
        lines.append("IPI_REQ_YIELD_CORE core=1")
    lines.append("NOTIFY_POST result=0x1")
    for _ in range(ipi_total_extra):
        lines.append("IPI_REQ_SEND core=1 reason=0x4")
    for _ in range(taken_isr):
        lines.append("IPI_TAKEN_ISR")
    for _ in range(sem_give):
        lines.append("SEMAPHORE_GIVE_FROM_ISR obj=0x%x" % sem_give_obj)
    lines += [
        "TCB_NAME=ActivityRender",
        "TCB_PRIORITY=%d" % prio,
        "TCB_COREID=%d" % core,
        "TCB_NOTIFIED_VALUE=%d" % notified,
        "TCB_NOTIFY_STATE=%d" % notify_state,
        "TCB_STATE_CONTAINER=0x%x" % state,
        "TCB_EVENT_CONTAINER=0x%x" % event,
        "ADDR_DELAYED1=0x%x" % DELAYED,
        "ADDR_READY_AT_PRIO=0x%x" % READY,
        "READY_AT_PRIO_ITEMS=0",
        "ADDR_PENDING_READY_0=0x%x" % PENDING0,
        "ADDR_PENDING_READY_1=0x%x" % PENDING1,
        "PENDING_READY_0=%d" % pend_items,
        "PENDING_READY_1=%d" % pend_items,
        "SCHED_SUSPENDED_0=%d" % suspended,
        "SCHED_SUSPENDED_1=%d" % suspended,
        "SEM_HANDLE=0x3fcec294",
        "SEM_QUEUE_TYPE=%d" % sem_type,
        "SEM_LENGTH=1",
        "SEM_ITEM_SIZE=0",
        "SEM_MESSAGES=%d" % messages,
        "SEM_WAIT_LIST=0x%x" % WAIT_LIST,
        "SEM_WAITERS=%d" % waiters,
        "FROM_CPU_LEVELS=0 0 0 0",
        "FROM_CPU_MAP_CORE0=6 6 6 6",
        "FROM_CPU_MAP_CORE1=6 6 6 6",
    ]
    return "\n".join(lines) + "\n"


def qemu(sets=0, asserted=0, unrouted=0, map_writes=100):
    return {
        "cpu_intr_sets": [sets, 0, 0, 0],
        "from_cpu_asserted": [asserted, asserted],
        "unrouted": [unrouted, unrouted],
        "asserted": [asserted, asserted],
        "map_writes": map_writes,
    }


class CrosscoreIpiVerdictTests(unittest.TestCase):
    def test_parses_labels_counts_and_notify_window(self):
        obs = parse_probe_log(log(ipi_window=1, ipi_total_extra=2, taken_isr=3, sem_give=1))
        self.assertEqual(obs["IPI_REQ_IN_NOTIFY_WINDOW"], 1)
        self.assertEqual(obs["IPI_REQ_YIELD_CORE"], 1)
        self.assertEqual(obs["IPI_REQ_SEND"], 2)
        self.assertEqual(obs["IPI_TAKEN_ISR"], 3)
        self.assertEqual(obs["SEM_GIVE_OBJS"], [QUEUE])
        self.assertEqual(obs["TCB_STATE_CONTAINER"], DELAYED)
        self.assertEqual(obs["SEM_QUEUE_TYPE"], 3)
        self.assertEqual(obs["FROM_CPU_MAP_CORE1"], [6, 6, 6, 6])

    def test_observed_stall_is_a_semaphore_wait_not_a_lost_ipi(self):
        obs = parse_probe_log(log())
        result = classify(obs, qemu())
        self.assertEqual(result["verdict"], "unrelated_semaphore_wait")
        self.assertTrue(any("binary semaphore" in text for text in result["reasons"]))

    def test_missing_give_probe_names_the_absent_completion_interrupt(self):
        result = classify(parse_probe_log(log(sem_give=0)))
        self.assertTrue(any("panel completion interrupt" in text for text in result["reasons"]))

    def test_gives_of_other_objects_do_not_explain_the_wait(self):
        obs = parse_probe_log(log(sem_give=2, sem_give_obj=0x3FCBC460))
        result = classify(obs)
        self.assertEqual(result["verdict"], "unrelated_semaphore_wait")
        self.assertTrue(any("2 give(s) of other objects" in text for text in result["reasons"]))

    def test_give_of_the_same_handle_marks_a_racy_dump(self):
        result = classify(parse_probe_log(log(sem_give=1)))
        self.assertTrue(any("raced the give" in text for text in result["reasons"]))

    def test_requested_ipi_without_emulator_record_is_a_model_gap(self):
        result = classify(parse_probe_log(log(ipi_window=1)), qemu(sets=0))
        self.assertEqual(result["verdict"], "ipi_requested_but_not_recorded_by_emulator")

    def test_recorded_trigger_that_never_reached_the_matrix_is_a_wiring_gap(self):
        result = classify(parse_probe_log(log(ipi_window=1)), qemu(sets=5, asserted=0))
        self.assertEqual(result["verdict"], "ipi_requested_but_not_recorded_by_emulator")
        self.assertTrue(any("wiring" in text for text in result["reasons"]))

    def test_unrouted_matrix_value_is_reported_as_routing_failure(self):
        result = classify(parse_probe_log(log(ipi_window=1)), qemu(sets=5, asserted=4, unrouted=4))
        self.assertEqual(result["verdict"], "ipi_reached_matrix_but_unrouted_on_target_cpu")

    def test_routed_value_reaching_a_valid_extint_is_not_unrouted(self):
        text = log(ipi_window=1, taken_isr=1).replace("FROM_CPU_MAP_CORE1=6 6 6 6",
                                                      "FROM_CPU_MAP_CORE1=16 16 16 16")
        result = classify(parse_probe_log(text), qemu(sets=5, asserted=4, unrouted=0))
        self.assertEqual(result["verdict"], "ipi_delivered_but_cpu_kept_idle")

    def test_ready_task_blocked_by_pending_or_suspend_is_not_an_interrupt_issue(self):
        obs = parse_probe_log(log(state=READY, event=PENDING1, pend_items=1))
        self.assertEqual(classify(obs)["verdict"], "ready_but_not_dispatched_despite_ticks")

    def test_waiting_notification_without_ipi_needs_the_core_question(self):
        result = classify(parse_probe_log(log(notify_state=1)))
        self.assertEqual(result["verdict"], "insufficient_evidence")
        self.assertTrue(any("share a core" in text for text in result["reasons"]))

    def test_no_notify_return_is_insufficient(self):
        result = classify(parse_probe_log("boot only, nothing hit\n"))
        self.assertEqual(result["verdict"], "insufficient_evidence")

    def test_membership_falls_back_for_unknown_lists(self):
        result = classify(parse_probe_log(log(state=0x3FCABC00, event=0)))
        self.assertEqual(result["verdict"], "insufficient_evidence")
        self.assertTrue(any("other (0x3fcabc00)" in text for text in result["reasons"]))

    def test_gdb_only_run_flags_missing_counters(self):
        result = classify(parse_probe_log(log()), None)
        self.assertTrue(any("QEMU counters not supplied" in text for text in result["reasons"]))


if __name__ == "__main__":
    unittest.main()
