"""Verdict for the X4 Pro render stall: lost cross-core IPI or unrelated wait.

Consumes the labelled token lines emitted by
experiments/0043-x4pro-ipi-vs-semaphore.gdb and, optionally, the read-only QEMU
counters exposed by experiments/0043-s3-optin-crosscore-ipi-counters.patch.
Pure host logic: no QEMU access, no timing assumptions, no guest mutation.

Optional second input is a small JSON object assembled from the QOM properties, with
per-device-index lists: {"cpu_intr_sets": [0,0,0,0], "from_cpu_asserted": [0,0],
"unrouted": [0,0], "asserted": [0,0], "map_writes": 0}. Rules that need it are skipped
when it is absent, so a GDB-only run still reports what the kernel state alone proves.

FreeRTOS facts the classification relies on (IDF 5.5.5, configNUMBER_OF_CORES=2):
xTaskGenericNotify returns pdPASS unconditionally for eIncrement, latches
ucNotifyState to NOTIFICATION_RECEIVED (2), and only moves the task to the ready
list when the state it found was WAITING (1). A remote wake is therefore the
exception, not the rule, so "notify succeeded" never proves an IPI was sent.
"""
import json
import re
import sys
from pathlib import Path

# dc232b external interrupt numbers; 6 and 7 are NOT external interrupts, so an
# interrupt matrix entry left at QEMU's reset value 6 can never reach a CPU.
VALID_EXTINTS = {0, 1, 2, 3, 4, 5, 8, 9, 12, 14, 15, 16, 17, 18, 19, 20, 21}
BINARY_SEMAPHORE = 3
NOTIFY_WAITING = 1

_HEX = {
    "NOTIFY_RESULT": r"NOTIFY_POST result=0x([0-9a-f]+)",
    "TCB_STATE_CONTAINER": r"TCB_STATE_CONTAINER=0x([0-9a-f]+)",
    "TCB_EVENT_CONTAINER": r"TCB_EVENT_CONTAINER=0x([0-9a-f]+)",
    "ADDR_DELAYED1": r"ADDR_DELAYED1=0x([0-9a-f]+)",
    "ADDR_PENDING_READY_0": r"ADDR_PENDING_READY_0=0x([0-9a-f]+)",
    "ADDR_PENDING_READY_1": r"ADDR_PENDING_READY_1=0x([0-9a-f]+)",
    "ADDR_READY_AT_PRIO": r"ADDR_READY_AT_PRIO=0x([0-9a-f]+)",
    "SEM_HANDLE": r"SEM_HANDLE=0x([0-9a-f]+)",
    "SEM_WAIT_LIST": r"SEM_WAIT_LIST=0x([0-9a-f]+)",
}
_DEC = {
    "TCB_PRIORITY": r"TCB_PRIORITY=(\d+)",
    "TCB_COREID": r"TCB_COREID=(-?\d+)",
    "TCB_NOTIFIED_VALUE": r"TCB_NOTIFIED_VALUE=(\d+)",
    "TCB_NOTIFY_STATE": r"TCB_NOTIFY_STATE=(\d+)",
    "PENDING_READY_0": r"PENDING_READY_0=(\d+)",
    "PENDING_READY_1": r"PENDING_READY_1=(\d+)",
    "READY_AT_PRIO_ITEMS": r"READY_AT_PRIO_ITEMS=(\d+)",
    "SCHED_SUSPENDED_0": r"SCHED_SUSPENDED_0=(\d+)",
    "SCHED_SUSPENDED_1": r"SCHED_SUSPENDED_1=(\d+)",
    "SEM_QUEUE_TYPE": r"SEM_QUEUE_TYPE=(\d+)",
    "SEM_LENGTH": r"SEM_LENGTH=(\d+)",
    "SEM_MESSAGES": r"SEM_MESSAGES=(\d+)",
    "SEM_WAITERS": r"SEM_WAITERS=(\d+)",
}
_COUNT = {
    "IPI_REQ_YIELD_CORE": r"IPI_REQ_YIELD_CORE core=",
    "IPI_REQ_SEND_YIELD": r"IPI_REQ_SEND_YIELD core=",
    "IPI_REQ_SEND": r"IPI_REQ_SEND core=",
    "IPI_TAKEN_ISR": r"IPI_TAKEN_ISR",
}


def parse_probe_log(text):
    obs = {}
    for name, pattern in _HEX.items():
        found = re.findall(pattern, text)
        if found:
            obs[name] = int(found[-1], 16)
    for name, pattern in _DEC.items():
        found = re.findall(pattern, text)
        if found:
            obs[name] = int(found[-1])
    for name, pattern in _COUNT.items():
        obs[name] = len(re.findall(pattern, text))
    obs["SEM_GIVE_OBJS"] = [int(v, 16) for v in
                            re.findall(r"SEMAPHORE_GIVE_FROM_ISR obj=0x([0-9a-f]+)", text)]
    levels = re.findall(r"FROM_CPU_LEVELS=((?:\d+ ){3}\d+)", text)
    if levels:
        obs["FROM_CPU_LEVELS"] = [int(v) for v in levels[-1].split()]
    for core in (0, 1):
        rows = re.findall(r"FROM_CPU_MAP_CORE%d=((?:\d+ ){3}\d+)" % core, text)
        if rows:
            obs["FROM_CPU_MAP_CORE%d" % core] = [int(v) for v in rows[-1].split()]
    # Tokens emitted between the notify call and its return are the ones the
    # notify path itself produced; that window decides "IPI requested or not".
    pre = text.rfind("NOTIFY_PRE ")
    post = text.find("NOTIFY_POST ", pre)
    window = text[pre:post] if pre >= 0 and post > pre else ""
    obs["IPI_REQ_IN_NOTIFY_WINDOW"] = len(
        re.findall(r"IPI_REQ_(?:YIELD_CORE|SEND_YIELD|SEND)", window)
    )
    return obs


def _total(value):
    if value is None:
        return None
    return sum(value) if isinstance(value, list) else value


def classify(obs, qemu=None):
    reasons = []
    if "NOTIFY_RESULT" not in obs:
        return {
            "verdict": "insufficient_evidence",
            "reasons": ["probe never reached the xTaskGenericNotify return site"],
            "next_probe": "confirm the tap fired and re-check the 0x4210225d breakpoint address",
        }
    requested = obs.get("IPI_REQ_IN_NOTIFY_WINDOW", 0)
    req_total = (obs.get("IPI_REQ_YIELD_CORE", 0) + obs.get("IPI_REQ_SEND_YIELD", 0)
                 + obs.get("IPI_REQ_SEND", 0))
    sets = _total((qemu or {}).get("cpu_intr_sets"))
    asserted = _total((qemu or {}).get("from_cpu_asserted"))
    unrouted = _total((qemu or {}).get("unrouted"))
    state = obs.get("TCB_STATE_CONTAINER")
    delayed = obs.get("ADDR_DELAYED1")
    wait_list = obs.get("SEM_WAIT_LIST")
    ready = obs.get("ADDR_READY_AT_PRIO")
    pending = (obs.get("ADDR_PENDING_READY_0"), obs.get("ADDR_PENDING_READY_1"))
    pend_items = max(obs.get("PENDING_READY_0", 0), obs.get("PENDING_READY_1", 0))

    if requested:
        if sets == 0:
            reasons.append("the notify path asked for a remote wake %d times but no "
                           "SYSTEM_CPU_INTR_FROM_CPU_* write was recorded" % requested)
            return _verdict("ipi_requested_but_not_recorded_by_emulator", reasons, qemu)
        if asserted == 0:
            reasons.append("FROM_CPU bits were set %d times yet the interrupt matrix saw "
                           "no source pulse: the clock-to-matrix wiring is broken" % sets)
            return _verdict("ipi_requested_but_not_recorded_by_emulator", reasons, qemu)
        target = obs.get("FROM_CPU_MAP_CORE%d" % max(obs.get("TCB_COREID", 0), 0))
        if unrouted or (target and not any(v in VALID_EXTINTS for v in target)):
            reasons.append("matrix took the pulse but could not route it: map values %s, "
                           "unrouted asserts %s (6 is QEMU's reset value and is not a "
                           "dc232b external interrupt)" % (target, unrouted))
            return _verdict("ipi_reached_matrix_but_unrouted_on_target_cpu", reasons, qemu)
        if obs.get("IPI_TAKEN_ISR", 0):
            reasons.append("target CPU ran esp_crosscore_isr, so delivery worked; the stall "
                           "is downstream in INTENABLE, the tick, or the ready list")
            return _verdict("ipi_delivered_but_cpu_kept_idle", reasons, qemu)
        reasons.append("IPI requested but no delivery evidence; counters are partial")
        return _verdict("insufficient_evidence", reasons, qemu)

    # No remote wake was requested around the notify at all. Explain why.
    if obs.get("TCB_NOTIFY_STATE") == NOTIFY_WAITING:
        reasons.append("the task was notification-waiting, so the wake branch did run; "
                       "with no IPI token either both tasks share a core or the probes "
                       "were disabled too late")
        return _verdict("insufficient_evidence", reasons, qemu)
    if state is not None and state == ready and (pend_items or obs.get("SCHED_SUSPENDED_0")
                                                 or obs.get("SCHED_SUSPENDED_1")):
        reasons.append("task is on its ready list (priority %s, %d items) while pending "
                       "or scheduler-suspended counters hold dispatch back: this is "
                       "scheduling, not an interrupt"
                       % (obs.get("TCB_PRIORITY"), obs.get("READY_AT_PRIO_ITEMS")))
        return _verdict("ready_but_not_dispatched_despite_ticks", reasons, qemu)
    if (state is not None and state == delayed
            and obs.get("SEM_MESSAGES", 0) == 0
            and (obs.get("TCB_EVENT_CONTAINER") == wait_list
                 or obs.get("SEM_WAITERS", 0))):
        reasons.append("notification only latched (value %s, state %s) and produced no wake "
                       "request; the task is on xDelayedTaskList1 and its event item is on "
                       "the queue's waiter list (type %s, length %s, waiters %s)"
                       % (obs.get("TCB_NOTIFIED_VALUE"), obs.get("TCB_NOTIFY_STATE"),
                          obs.get("SEM_QUEUE_TYPE"), obs.get("SEM_LENGTH"),
                          obs.get("SEM_WAITERS")))
        if obs.get("SEM_QUEUE_TYPE") == BINARY_SEMAPHORE:
            reasons.append("the blocking object is a binary semaphore, not a task "
                           "notification")
        gives = obs.get("SEM_GIVE_OBJS", [])
        if obs.get("SEM_HANDLE") in gives:
            reasons.append("the blocking semaphore was given inside the window, so this dump "
                           "raced the give; re-run with a longer stall window")
        else:
            reasons.append("no xQueueGiveFromISR for handle 0x%x landed in the window "
                           "(%d give(s) of other objects): the panel completion interrupt "
                           "is missing" % (obs.get("SEM_HANDLE", 0), len(gives)))
        return _verdict("unrelated_semaphore_wait", reasons, qemu)
    reasons.append("no IPI request and the membership does not match a notification wait: "
                   "state list is %s, %d cross-core requests in the whole log"
                   % (_membership(state, delayed, ready, pending), req_total))
    return _verdict("insufficient_evidence", reasons, qemu)


def _membership(state, delayed, ready, pending):
    if state is None:
        return "unknown"
    if state == delayed:
        return "delayed"
    if state == ready:
        return "ready"
    if state in pending:
        return "pending ready"
    return "other (0x%x)" % state


def _verdict(name, reasons, qemu):
    if qemu is None:
        reasons = reasons + ["QEMU counters not supplied; pass the counters JSON as the "
                             "second argument to separate guest and emulator responsibility"]
    hint = {
        "ipi_requested_but_not_recorded_by_emulator": "inspect hw/xtensa/esp32s3_clk.c "
            "FROM_CPU write path and esp32s3.c irq wiring",
        "ipi_reached_matrix_but_unrouted_on_target_cpu": "fix the reset routing: QEMU maps "
            "every source to 6, which is not a dc232b external interrupt (real default is 16)",
        "ipi_delivered_but_cpu_kept_idle": "read per-CPU INTSET/INTENABLE from the monitor, "
            "then the xTaskIncrementTick drain",
        "unrelated_semaphore_wait": "model the panel completion interrupt that gives the "
            "semaphore; do not inject interrupts to force progress",
        "ready_but_not_dispatched_despite_ticks": "check spinlock/scheduler-suspend nesting "
            "on the core that owns the ready list",
        "insufficient_evidence": "re-run with the opt-in counters patch and a longer stall window",
    }
    return {"verdict": name, "reasons": reasons, "next_probe": hint[name]}


def main(argv):
    if len(argv) < 2:
        print("usage: crosscore_ipi_verdict.py <probe.log> [<qemu-counters.json>]",
              file=sys.stderr)
        return 2
    obs = parse_probe_log(Path(argv[1]).read_text(errors="replace"))
    qemu = json.loads(Path(argv[2]).read_text()) if len(argv) > 2 else None
    result = classify(obs, qemu)
    print("VERDICT %s" % result["verdict"])
    for line in result["reasons"]:
        print("  - %s" % line)
    print("  next: %s" % result["next_probe"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
