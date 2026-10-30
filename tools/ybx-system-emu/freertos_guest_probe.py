#!/usr/bin/env python3
"""Read-only FreeRTOS/EPD guest RAM decoding over QEMU's HMP `xp` command.

Every address and offset here was extracted from the DWARF of the matching
`.pio/build/x4pro/firmware.elf` for the ORIGINAL X4 Pro 092c2be5 factory
build. They are not generic: callers must gate on
`FACTORY_092C2BE5_FLASH_SHA256` and report unavailable data instead of
guessing. Nothing in this module writes guest memory, and no result here is
proof of a visible UI change — only the real SSD1677 RAM comparison is.
"""
import re

FACTORY_092C2BE5_FLASH_SHA256 = (
    "206ed9661bd71477d1a54397c5a2cfe490179a3f59ab094f1e7421745c609523"
)

# tasks.c statics, in address order. `pxDelayedTaskList` points into the
# adjacent xDelayedTaskList1/2 pair, and the ready-list array ends exactly at
# pxCurrentTCBs; both are cheap cross-checks that the guest matches this ELF.
KERNEL = {
    "xSchedulerRunning": 0x3FCABF8C,
    "xPendingReadyList": 0x3FCABFC8,      # [2] List_t, one per core
    "pxDelayedTaskList": 0x3FCABFF4,      # List_t *
    "xDelayedTaskList2": 0x3FCABFF8,
    "xDelayedTaskList1": 0x3FCAC00C,
    "pxReadyTasksLists": 0x3FCAC020,      # [25] List_t
    "pxCurrentTCBs": 0x3FCAC214,          # [2] TCB_t *
}
MAX_PRIORITIES = 25
CORES = 2

# EpdBus.cpp:74 file-static semaphore handle (the BUSY-pin ISR gives it).
EPD_REFRESH_DONE_HANDLE = 0x3FCA1DA8
# ESP32-S3 GPIO matrix registers and the X4 Pro panel BUSY line.
GPIO_IN_REG = 0x6000403C
GPIO_STATUS_REG = 0x60004044
GPIO_ENABLE_REG = 0x60004020
GPIO_PIN6_CFG_REG = 0x6000408C
EPD_BUSY_GPIO = 6

LIST_ITEM = {"x_item_value": 0x00, "px_next": 0x04, "px_previous": 0x08,
             "pv_owner": 0x0C, "pv_container": 0x10}
LIST = {"ux_number_of_items": 0x00, "px_index": 0x04, "x_list_end": 0x08}
LIST_SIZE = 20
MINI_END_NEXT = 0x0C  # xListEnd.pxNext, as an offset inside List_t

TCB = {"px_top_of_stack": 0x00, "x_state_list_item": 0x04,
       "x_event_list_item": 0x18, "ux_priority": 0x2C, "px_stack": 0x30,
       "pc_task_name": 0x34, "x_core_id": 0x44, "px_end_of_stack": 0x48,
       "ux_tcb_number": 0x4C, "ux_task_number": 0x50, "ux_base_priority": 0x54,
       "ux_mutexes_held": 0x58, "ul_run_time_counter": 0x64,
       "ul_notified_value": 0x158, "uc_notify_state": 0x15C}
TCB_SIZE = 352

QUEUE = {"pc_head": 0x00, "pc_write_to": 0x04, "x_mutex_holder": 0x08,
         "ux_recursive_call_count": 0x0C, "x_tasks_waiting_to_send": 0x10,
         "x_tasks_waiting_to_receive": 0x24, "ux_messages_waiting": 0x38,
         "ux_length": 0x3C, "ux_item_size": 0x40, "ux_queue_number": 0x4C,
         "uc_queue_type": 0x50}
QUEUE_SIZE = 92
QUEUE_TYPES = {0: "queue", 1: "mutex", 2: "counting-semaphore",
               3: "binary-semaphore", 4: "recursive-mutex"}
# Raw kernel bookkeeping byte; V10.x convention, reported with its number too.
NOTIFY_STATES = {0: "no-notification-state", 1: "task-waiting-for-notification",
                 2: "notification-recorded-not-consumed"}

MAX_LIST_ITEMS = 64
MAX_ENUMERATED_TASKS = 96
DRAM_LO, DRAM_HI = 0x3FC88000, 0x3FD00000
WORD_LINE = re.compile(r"^\s*([0-9a-fA-F]{1,16}):\s*(.*)$")
WORD_TOKEN = re.compile(r"0x([0-9a-fA-F]+)")


def parse_xp_words(reply, address, count):
    """Decode HMP `xp /<count>wx <address>` output into `count` words.

    Each printed line starts with the address of its first value, so the line
    headers carry the placement and any QEMU line wrapping works. Returns None
    when the reply is an error or does not cover every requested word.
    """
    if not isinstance(reply, str):
        return None
    found = {}
    for line in reply.splitlines():
        header = WORD_LINE.match(line)
        if not header:
            continue
        base = int(header.group(1), 16)
        for index, token in enumerate(WORD_TOKEN.findall(header.group(2))):
            found[base + 4 * index] = int(token, 16)
    words = [found.get(address + 4 * index) for index in range(count)]
    return words if all(word is not None for word in words) else None


class GuestRam:
    """Cached 32-bit guest RAM reads. Read failures are recorded, never 0."""

    def __init__(self, qmp, chunk_words=8):
        self.qmp = qmp
        self.chunk = chunk_words
        self.words = {}
        self.container_labels = {}
        self.errors = []

    def _fetch(self, address, count):
        words = []
        end = address + 4 * count
        for start in range(address, end, 4 * self.chunk):
            n = min(4 * self.chunk, end - start) // 4
            reply = self.qmp("human-monitor-command",
                             {"command-line": f"xp /{n}wx 0x{start:08x}"})
            got = parse_xp_words(reply.get("return", reply), start, n)
            if got is None:
                self.errors.append({"address": hex(start), "words": n,
                                    "reply": str(reply)[:200]})
                return False
            words.extend(got)
        for offset, value in zip(range(address, end, 4), words):
            self.words[offset] = value
        return True

    def u32(self, address):
        address -= address % 4
        if address not in self.words and not self._fetch(address, self.chunk):
            return None
        return self.words.get(address)

    def block(self, address, length):
        start = address - (address % 4)
        words = (address - start) + (length + 3) // 4
        if not self._fetch(start, words):
            return None
        out = bytearray()
        for offset in range(start, start + 4 * words, 4):
            value = self.words.get(offset)
            if value is None:
                return None
            out += value.to_bytes(4, "little")
        return bytes(out[address - start:address - start + length])


def _plausible_pointer(value):
    return value is not None and DRAM_LO <= value < DRAM_HI


def task_name(ram, tcb_address):
    raw = ram.block(tcb_address + TCB["pc_task_name"], 16)
    if raw is None:
        return None
    text = raw.split(b"\0", 1)[0]
    if not text or any(byte < 0x20 or byte > 0x7E for byte in text):
        return None
    return text.decode("ascii")


def read_task(ram, tcb_address):
    """Decode the parts of a TCB that answer "why has this task not run?"."""
    if not _plausible_pointer(tcb_address):
        return None
    fields = ["ux_priority", "ux_base_priority", "ux_mutexes_held",
              "ux_tcb_number", "x_core_id", "ul_notified_value",
              "ul_run_time_counter"]
    values = {key: ram.u32(tcb_address + TCB[key]) for key in fields}
    if any(value is None for value in values.values()):
        return None
    name = task_name(ram, tcb_address)
    if name is None:
        return None
    notify_state = ram.u32(tcb_address + TCB["uc_notify_state"])
    if notify_state is None:
        return None
    notify_state &= 0xFF
    state_item = tcb_address + TCB["x_state_list_item"]
    event_item = tcb_address + TCB["x_event_list_item"]
    return {
        "tcb": hex(tcb_address),
        "name": name,
        "ux_priority": values["ux_priority"],
        "ux_base_priority": values["ux_base_priority"],
        "ux_mutexes_held": values["ux_mutexes_held"],
        "ux_tcb_number": values["ux_tcb_number"],
        "x_core_id": values["x_core_id"],
        "ul_notified_value": values["ul_notified_value"],
        "ul_run_time_counter": values["ul_run_time_counter"],
        "uc_notify_state": notify_state,
        "uc_notify_state_meaning": NOTIFY_STATES.get(
            notify_state, "unknown-value"),
        "state_item": describe_item(ram, state_item),
        "event_item": describe_item(ram, event_item),
    }


def describe_item(ram, item_address):
    container = ram.u32(item_address + LIST_ITEM["pv_container"])
    item_value = ram.u32(item_address + LIST_ITEM["x_item_value"])
    owner = ram.u32(item_address + LIST_ITEM["pv_owner"])
    return {"item": hex(item_address), "x_item_value": item_value,
            "pv_owner": _hex(owner), "pv_container": _hex(container),
            "container_kind": container_kind(ram, container)}


def container_kind(ram, container):
    """Name the kernel object that owns a list, from the validated statics."""
    if container is None:
        return None
    if container in ram.container_labels:
        return ram.container_labels[container]
    label = _container_kind(ram, container)
    ram.container_labels[container] = label
    return label


def _container_kind(ram, container):
    if container == 0:
        return "unlinked"
    ready = KERNEL["pxReadyTasksLists"]
    if ready <= container < ready + MAX_PRIORITIES * LIST_SIZE and \
            (container - ready) % LIST_SIZE == 0:
        return f"ready-priority-{(container - ready) // LIST_SIZE}"
    if container == KERNEL["xDelayedTaskList1"]:
        return "delayed-list-1"
    if container == KERNEL["xDelayedTaskList2"]:
        return "delayed-list-2"
    pending = KERNEL["xPendingReadyList"]
    if pending <= container < pending + CORES * LIST_SIZE and \
            (container - pending) % LIST_SIZE == 0:
        return f"pending-ready-core-{(container - pending) // LIST_SIZE}"
    if ram.u32(KERNEL["pxDelayedTaskList"]) == container:
        return "delayed-list-active-pointer"
    if DRAM_LO <= container < DRAM_HI:
        for offset, label in ((QUEUE["x_tasks_waiting_to_receive"],
                               "waiting-to-receive"),
                              (QUEUE["x_tasks_waiting_to_send"],
                               "waiting-to-send")):
            queue = container - offset
            kind = queue_type(ram, queue) if queue >= DRAM_LO else None
            if kind:
                return f"queue@{queue:08x}.{label}:{kind}"
        return "other-list"
    return "outside-dram"


def queue_type(ram, address):
    """QUEUE_TYPES only if the words at that base are structurally sane."""
    kind = ram.u32(address + QUEUE["uc_queue_type"])
    item_size = ram.u32(address + QUEUE["ux_item_size"])
    length = ram.u32(address + QUEUE["ux_length"])
    waiting = ram.u32(address + QUEUE["ux_messages_waiting"])
    if kind is None or item_size is None or length is None or waiting is None:
        return None
    if kind not in QUEUE_TYPES or item_size > 64 or not 1 <= length <= 4096:
        return None
    if waiting > length:
        return None
    return QUEUE_TYPES[kind]


def read_queue(ram, address):
    """Decode a Queue_t. Returns None unless the layout checks pass."""
    kind = queue_type(ram, address)
    if not kind:
        return None
    holder = ram.u32(address + QUEUE["x_mutex_holder"])
    # The union word is only a holder for mutexes; a binary semaphore leaves
    # it unused, so it is never decoded as a task pointer.
    mutex_holder = _hex(holder) if holder is not None and \
        kind in ("mutex", "recursive-mutex") else None
    return {
        "queue": hex(address),
        "kind": kind,
        "uc_queue_type": ram.u32(address + QUEUE["uc_queue_type"]),
        "ux_messages_waiting": ram.u32(address + QUEUE["ux_messages_waiting"]),
        "ux_length": ram.u32(address + QUEUE["ux_length"]),
        "ux_item_size": ram.u32(address + QUEUE["ux_item_size"]),
        "ux_queue_number": ram.u32(address + QUEUE["ux_queue_number"]),
        "x_mutex_holder": mutex_holder,
        "waiters_receiving": read_waiters(
            ram, address + QUEUE["x_tasks_waiting_to_receive"]),
        "waiters_sending": read_waiters(
            ram, address + QUEUE["x_tasks_waiting_to_send"]),
    }


def read_waiters(ram, list_address):
    items = ram.u32(list_address + LIST["ux_number_of_items"])
    end = list_address + LIST["x_list_end"]
    nxt = ram.u32(list_address + MINI_END_NEXT)
    if items is None or nxt is None:
        return None
    walkers, visited = [], set()
    limit = min(items, MAX_LIST_ITEMS)  # never trust a kernel count blindly
    while nxt != end and nxt not in visited and len(walkers) < limit:
        visited.add(nxt)
        if not _plausible_pointer(nxt):
            break
        owner = ram.u32(nxt + LIST_ITEM["pv_owner"])
        value = ram.u32(nxt + LIST_ITEM["x_item_value"])
        if owner is None or value is None:
            break
        if _plausible_pointer(owner):
            task = read_task(ram, owner)
            walkers.append({"owner": hex(owner),
                            "name": task["name"] if task else None})
        else:
            walkers.append({"owner": hex(owner), "name": None})
        nxt = ram.u32(nxt + LIST_ITEM["px_next"])
    return {"ux_number_of_items": items, "walked": walkers}


def task_row(ram, tcb_address):
    """Compact scheduler-table row: enough to see where each task is parked."""
    name = task_name(ram, tcb_address)
    priority = ram.u32(tcb_address + TCB["ux_priority"])
    core = ram.u32(tcb_address + TCB["x_core_id"])
    notify = ram.u32(tcb_address + TCB["ul_notified_value"])
    state = ram.u32(tcb_address + TCB["uc_notify_state"])
    state_list = ram.u32(tcb_address + TCB["x_state_list_item"] +
                         LIST_ITEM["pv_container"])
    event_list = ram.u32(tcb_address + TCB["x_event_list_item"] +
                         LIST_ITEM["pv_container"])
    if None in (name, priority, core, notify, state, state_list, event_list):
        return None
    return {"tcb": hex(tcb_address), "name": name, "ux_priority": priority,
            "x_core_id": core, "ul_notified_value": notify,
            "uc_notify_state": state & 0xFF,
            "state_list": container_kind(ram, state_list),
            "event_list": container_kind(ram, event_list)}


def enumerate_tasks(ram):
    """Every task reachable from the scheduler lists, no hardcoded TCB address."""
    rows, seen = [], set()
    lists = [(f"ready-priority-{priority}",
              KERNEL["pxReadyTasksLists"] + priority * LIST_SIZE)
             for priority in range(MAX_PRIORITIES)]
    lists += [("delayed-list-1", KERNEL["xDelayedTaskList1"]),
              ("delayed-list-2", KERNEL["xDelayedTaskList2"])]
    lists += [(f"pending-ready-core-{core}",
               KERNEL["xPendingReadyList"] + core * LIST_SIZE)
              for core in range(CORES)]
    for label, list_address in lists:
        end = list_address + LIST["x_list_end"]
        nxt = ram.u32(list_address + MINI_END_NEXT)
        steps = 0
        while nxt is not None and nxt != end and steps < MAX_LIST_ITEMS:
            if not _plausible_pointer(nxt):
                break
            owner = ram.u32(nxt + LIST_ITEM["pv_owner"])
            steps += 1
            if owner is None or owner in seen or not _plausible_pointer(owner):
                break
            seen.add(owner)
            row = task_row(ram, owner)
            if row:
                row["found_in"] = label
                rows.append(row)
            nxt = ram.u32(nxt + LIST_ITEM["px_next"])
        if len(rows) >= MAX_ENUMERATED_TASKS:
            break
    return rows


def _hex(value):
    return hex(value) if value is not None else None


def task_matches(name, needle):
    """True if a task name is the requested one.

    The kernel stores at most 16 characters ("ActivityManagerRender" arrives
    as "ActivityManagerR"), so accept either prefix direction.
    """
    return bool(name) and (name == needle[:len(name)] or needle in name)


def snapshot(qmp, task_name_needle="ActivityManagerRender"):
    """One read-only FreeRTOS + EPD BUSY observation of the running guest."""
    ram = GuestRam(qmp)
    scheduler = ram.u32(KERNEL["xSchedulerRunning"])
    current = [ram.u32(KERNEL["pxCurrentTCBs"] + core * 4)
               for core in range(CORES)]
    epd_handle = ram.u32(EPD_REFRESH_DONE_HANDLE)
    busy_in = ram.u32(GPIO_IN_REG)
    busy_status = ram.u32(GPIO_STATUS_REG)
    busy_enable = ram.u32(GPIO_ENABLE_REG)
    tasks = enumerate_tasks(ram)
    row = next((item for item in tasks
                if task_matches(item["name"], task_name_needle)), None)
    target = read_task(ram, int(row["tcb"], 16)) if row else None
    record = {
        "available": scheduler is not None and target is not None,
        "reason": None,
        "x_scheduler_running": scheduler,
        "px_current_tcbs": [_hex(value) for value in current],
        "epd_refresh_done_semaphore_handle": _hex(epd_handle),
        "epd_busy_gpio": {
            "gpio": EPD_BUSY_GPIO,
            "input_reg": _hex(busy_in),
            "input_bit": (busy_in >> EPD_BUSY_GPIO) & 1
            if busy_in is not None else None,
            "interrupt_status_bit": (busy_status >> EPD_BUSY_GPIO) & 1
            if busy_status is not None else None,
            "output_enable_bit": (busy_enable >> EPD_BUSY_GPIO) & 1
            if busy_enable is not None else None,
            "pin_cfg_reg": _hex(ram.u32(GPIO_PIN6_CFG_REG)),
        },
        "task_count": len(tasks),
        "tasks": tasks,
        "render_task": target,
    }
    blocked = analyse_block(ram, target, epd_handle) if target else None
    record["render_task_block"] = blocked
    if record["available"]:
        record["reason"] = "decoded from validated factory 092c2be5 layout"
    else:
        record["reason"] = ("render task not reachable from scheduler lists"
                            if scheduler is not None else
                            "guest RAM read failed; wrong or unmatched build")
        if ram.errors:
            record["read_errors"] = ram.errors[:4]
    return record


def analyse_block(ram, task, epd_handle):
    """Where is the render task parked, and does the EPD semaphore explain it?"""
    kind = task["event_item"]["container_kind"] or ""
    state = task["state_item"]["container_kind"] or ""
    queue_address = None
    match = re.match(r"queue@([0-9a-f]{8})\.", kind)
    if match:
        queue_address = int(match.group(1), 16)
    queue = read_queue(ram, queue_address) if queue_address else None
    return {
        "state_list": state,
        "event_list": kind,
        "state_is_delayed": state in ("delayed-list-1", "delayed-list-2"),
        "blocked_on_queue": queue_address is not None,
        "blocked_queue": queue,
        "blocked_queue_matches_epd_refresh_done": bool(
            queue_address and epd_handle and
            queue_address == epd_handle),
        "holding_mutexes": task["ux_mutexes_held"],
        "notification_value": task["ul_notified_value"],
        "notify_state": task["uc_notify_state"],
    }


def assess_render_wake(before, after):
    """Strict classification of a pre/post tap pair. Never a UI claim."""
    ok = bool(before and after and before.get("available")
              and after.get("available"))
    def block(view):
        return (view or {}).get("render_task_block") or {}
    def task(view):
        return (view or {}).get("render_task") or {}
    pre, post = block(before), block(after)
    pre_task, post_task = task(before), task(after)
    same_tcb = bool(pre_task.get("tcb")) and pre_task.get("tcb") == \
        post_task.get("tcb")
    state = str(post.get("state_list", ""))
    running = bool(post_task.get("tcb")) and \
        post_task.get("tcb") in (after.get("px_current_tcbs") or [])
    woke = bool(ok and same_tcb and
                (running or state.startswith(("ready-priority",
                                              "pending-ready"))))
    return {
        "available": ok and same_tcb,
        "reason": "ok" if (ok and same_tcb) else
                  "both snapshots must decode the same render TCB",
        "notify_value_before": pre_task.get("ul_notified_value"),
        "notify_value_after": post_task.get("ul_notified_value"),
        "notification_latched": bool(
            ok and (pre_task.get("ul_notified_value") or 0) == 0 and
            (post_task.get("ul_notified_value") or 0) != 0),
        "notification_state_recorded": bool(
            ok and (post_task.get("uc_notify_state") or 0) !=
            (pre_task.get("uc_notify_state") or 0)),
        "blocked_on_binary_semaphore": bool(
            ok and post.get("blocked_on_queue") and
            (post.get("blocked_queue") or {}).get("kind") == "binary-semaphore"),
        "blocked_object_is_epd_refresh_done": bool(
            ok and post.get("blocked_queue_matches_epd_refresh_done")),
        "same_blocked_object": bool(
            ok and pre.get("event_list") and
            pre.get("event_list") == post.get("event_list")),
        "render_task_woken": woke,
        "render_task_state_list_after": post.get("state_list"),
        "still_parked_in_delayed_list": bool(
            ok and post.get("state_is_delayed")),
    }
