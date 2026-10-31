#!/usr/bin/env python3
"""Read-only FreeRTOS/EPD guest RAM decoding over QEMU's HMP `xp` command.

Addresses come from the `.pio/build/x4pro/firmware.elf` of the ORIGINAL X4 Pro
092c2be5 factory build. `KernelLayout.from_elf` discovers them, and the list
array dimensions, from that ELF's symbol table instead of trusting a literal,
so a build that does not match reports a layout error rather than decoding
garbage; `KernelLayout.recorded()` keeps the factory values for callers that
have no ELF. Callers must still gate on `FACTORY_092C2BE5_FLASH_SHA256` and
report unavailable data instead of guessing. Nothing in this module writes
guest memory, and no result here is proof of a visible UI change — only the
real SSD1677 RAM comparison is.
"""
import re
import struct

FACTORY_092C2BE5_FLASH_SHA256 = (
    "206ed9661bd71477d1a54397c5a2cfe490179a3f59ab094f1e7421745c609523"
)

# tasks.c and queue.c statics as recorded for the factory build, in address
# order. `pxDelayedTaskList` points into the adjacent xDelayedTaskList1/2 pair,
# and the ready-list array ends exactly at pxCurrentTCBs; both are cheap
# cross-checks that the guest matches this ELF.
#
# xSuspendedTaskList and xTasksWaitingTermination are the two lists the kernel
# itself walks when it enumerates tasks (uxTaskGetSystemState, at line 3006 and
# line 2998 of components/freertos/FreeRTOS-Kernel/tasks.c). Any task blocking
# with portMAX_DELAY is parked on xSuspendedTaskList, not on a delayed list
# (same file, 6381-6386), which is where most application tasks of a booted
# reader sit.
KERNEL = {
    "xSchedulerRunning": 0x3FCABF8C,
    "xTickCount": 0x3FCABF94,
    "xNextTaskUnblockTime": 0x3FCABF74,
    "xSuspendedTaskList": 0x3FCABF9C,        # List_t
    "xTasksWaitingTermination": 0x3FCABFB4,  # List_t
    "xPendingReadyList": 0x3FCABFC8,         # [2] List_t, one per core
    "pxDelayedTaskList": 0x3FCABFF4,         # List_t *
    "xDelayedTaskList2": 0x3FCABFF8,
    "xDelayedTaskList1": 0x3FCAC00C,
    "pxReadyTasksLists": 0x3FCAC020,         # [25] List_t
    "pxCurrentTCBs": 0x3FCAC214,             # [2] TCB_t *
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
# Every walk starts with these counters. Any other key is a finding, so a list
# that produced no tasks but raised one surprise still reaches the snapshot.
WALK_REPORT_BASE = frozenset(("items_walked", "duplicate-owner",
                              "implausible-owner", "unreadable-owner",
                              "undecodable-task"))
# Never-initialised lists are the pre-scheduler-start state of a build, not a
# decode failure, so they do not count as findings.
WALK_QUIET_FLAGS = frozenset(("never-initialised",))
DRAM_LO, DRAM_HI = 0x3FC88000, 0x3FD00000
WORD_LINE = re.compile(r"^\s*([0-9a-fA-F]{1,16}):\s*(.*)$")
WORD_TOKEN = re.compile(r"0x([0-9a-fA-F]+)")

# Symbol names the layout is built from. A List_t is LIST_SIZE bytes and a
# kernel counter is one word, which is what turns sizes into array bounds.
LIST_KEYS = ("xSuspendedTaskList", "xTasksWaitingTermination",
             "xPendingReadyList", "xDelayedTaskList1", "xDelayedTaskList2",
             "pxReadyTasksLists")
WORD_KEYS = ("xSchedulerRunning", "xTickCount", "xNextTaskUnblockTime",
             "pxDelayedTaskList")
# EpdBus.cpp file-static semaphore handle, mangled as freeink::s_epdRefreshDone.
EPD_SEMAPHORE_SYMBOL = "_ZN7freeinkL16s_epdRefreshDoneE"
TICK_SPACE = 0x100000000
# ELF symbol-type constants, spelled out because they are easy to mix up:
# STT_OBJECT is 1, STT_FUNC is 2.
_ELF_SHT_SYMTAB = 2
_ELF_STT_OBJECT = 1
# The two waits measured out of the factory disassembly of
# freeink::EpdBus::waitRefreshComplete (0x4201e968). pdMS_TO_TICKS folds to the
# literal at the take site, so 20 and 30000 are tick counts, and the only tick
# rate consistent with both is 1000 Hz.
EPD_WAIT_BRANCHES = (
    {"label": "epd-busy-poll-20ms", "ticks": 20,
     "source": "EpdBus.cpp:445", "take_site": "0x4201ea15"},
    {"label": "epd-busy-timeout-30s", "ticks": 30000,
     "source": "EpdBus.cpp:460", "take_site": "0x4201ea6e"},
)


class LayoutError(ValueError):
    """The supplied ELF does not describe this kernel layout; do not decode."""


def read_elf_object_symbols(path):
    """name -> [(address, size)] for every STT_OBJECT in a 32-bit LE ELF.

    pyelftools is not available in the offline builder and a third-party
    dependency is not allowed, so the two ELF structures needed here (the
    section header table and `.symtab`/`.strtab`) are unpacked directly.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as error:
        raise LayoutError(f"{path}: {error}") from None
    if len(data) < 0x34 or data[:4] != b"\x7fELF":
        raise LayoutError(f"{path}: not an ELF file")
    if data[4] != 1:
        raise LayoutError(f"{path}: EI_CLASS={data[4]}: only 32-bit ELFs are "
                          f"supported, so a 64-bit host binary is rejected")
    if data[5] != 1:
        raise LayoutError(f"{path}: big-endian ELFs are not supported")
    shoff, = struct.unpack_from("<I", data, 0x20)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x2E)
    if shentsize < 40 or shnum < 1 or shoff + shnum * shentsize > len(data):
        raise LayoutError(f"{path}: section header table runs past the file")
    if shstrndx >= shnum:
        raise LayoutError(f"{path}: e_shstrndx {shstrndx} is out of range")

    def section(index):
        base = shoff + index * shentsize
        return struct.unpack_from("<10I", data, base)

    strtab = section(shstrndx)
    if strtab[4] + strtab[5] > len(data):
        raise LayoutError(f"{path}: .shstrtab runs past the file")
    symbols = {}
    for index in range(shnum):
        (sh_name, sh_type, _flags, _addr, sh_offset, sh_size, sh_link, _info,
         _align, sh_entsize) = section(index)
        if sh_type != _ELF_SHT_SYMTAB:  # SHT_DYNSYM is deliberately ignored
            continue
        if sh_offset + sh_size > len(data):
            raise LayoutError(f"{path}: .symtab (index {index}) runs past "
                              f"the file")
        stride = sh_entsize if sh_entsize >= 16 else 16
        linked = section(sh_link) if 0 <= sh_link < shnum else None
        if linked is None or linked[4] + linked[5] > len(data):
            raise LayoutError(f"{path}: symtab {index} points outside the file")
        names_at, names_len = linked[4], linked[5]
        for entry in range(sh_offset, sh_offset + sh_size - stride + 1, stride):
            st_name, st_value, st_size, st_info = struct.unpack_from(
                "<IIIB", data, entry)
            if st_info & 0xF != _ELF_STT_OBJECT or not st_name or \
                    st_name >= names_len:
                continue
            raw = data[names_at + st_name:
                       names_at + min(names_len, st_name + 256)]
            name = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
            if name:
                symbols.setdefault(name, []).append((st_value, st_size))
    return symbols


class KernelLayout:
    """Kernel statics a decode needs, either recorded or discovered from ELF."""

    def __init__(self, addresses, max_priorities, cores, epd_refresh_done,
                 source):
        self.addresses = dict(addresses)
        self.max_priorities = max_priorities
        self.cores = cores
        self.epd_refresh_done = epd_refresh_done
        self.source = source

    def addr(self, key):
        try:
            return self.addresses[key]
        except KeyError:
            raise LayoutError(f"layout from {self.source} has no {key}") from None

    def ready_list(self, priority):
        return self.addr("pxReadyTasksLists") + priority * LIST_SIZE

    def pending_list(self, core):
        return self.addr("xPendingReadyList") + core * LIST_SIZE

    def current_tcb(self, core):
        return self.addr("pxCurrentTCBs") + core * 4

    def to_dict(self):
        return {"source": self.source,
                "max_priorities": self.max_priorities, "cores": self.cores,
                "epd_refresh_done": hex(self.epd_refresh_done),
                "addresses": {key: hex(value)
                              for key, value in sorted(self.addresses.items())}}

    @classmethod
    def recorded(cls):
        """The factory 092c2be5 numbers, for callers with no ELF on hand."""
        return cls(KERNEL, MAX_PRIORITIES, CORES, EPD_REFRESH_DONE_HANDLE,
                   "recorded factory 092c2be5 build")

    @classmethod
    def from_elf(cls, path):
        """Discover addresses and array bounds from a firmware ELF symtab.

        Every check below is a refusal, not a correction: an ELF whose symbol
        sizes do not look like this kernel's statics means a different build,
        and decoding it with borrowed offsets produces confident garbage.
        """
        symbols = read_elf_object_symbols(path)
        found = {}
        for key in LIST_KEYS + WORD_KEYS + ("pxCurrentTCBs",):
            entries = sorted(set(symbols.get(key, ())))
            if not entries:
                raise LayoutError(
                    f"{path}: no {key} symbol, so this is not an ELF of the "
                    f"FreeRTOS build being probed")
            if len(entries) > 1:
                raise LayoutError(
                    f"{path}: {key} is ambiguous between "
                    f"{[hex(value) for value, _ in entries]}")
            found[key] = entries[0]
        addresses = {}
        for key in WORD_KEYS:
            value, size = found[key]
            if size != 4:
                raise LayoutError(f"{path}: {key} is {size} bytes, not one word")
            addresses[key] = value
        for key in LIST_KEYS:
            value, size = found[key]
            if size == 0 or size % LIST_SIZE:
                raise LayoutError(f"{path}: {key} is {size} bytes, not a "
                                  f"whole number of {LIST_SIZE}-byte List_t")
            addresses[key] = value
        tcb_value, tcb_size = found["pxCurrentTCBs"]
        if tcb_size == 0 or tcb_size % 4:
            raise LayoutError(f"{path}: pxCurrentTCBs is {tcb_size} bytes, "
                              f"not an array of pointers")
        addresses["pxCurrentTCBs"] = tcb_value
        max_priorities = found["pxReadyTasksLists"][1] // LIST_SIZE
        cores = found["xPendingReadyList"][1] // LIST_SIZE
        if tcb_size // 4 != cores:
            raise LayoutError(f"{path}: pxCurrentTCBs holds {tcb_size // 4} "
                              f"pointers but xPendingReadyList implies {cores} "
                              f"cores")
        if not 1 <= max_priorities <= 256 or not 1 <= cores <= 8:
            raise LayoutError(f"{path}: derived bounds are implausible "
                              f"(max_priorities={max_priorities}, cores={cores})")
        for key, value in addresses.items():
            if value % 4 or not DRAM_LO <= value < DRAM_HI:
                raise LayoutError(f"{path}: {key} at 0x{value:08x} is not a "
                                  f"word-aligned DRAM address")
        epd = EPD_REFRESH_DONE_HANDLE
        source = f"discovered from {path}"
        entries = sorted(set(symbols.get(EPD_SEMAPHORE_SYMBOL, ())))
        if len(entries) == 1 and entries[0][0] % 4 == 0 and \
                DRAM_LO <= entries[0][0] < DRAM_HI:
            epd = entries[0][0]
        else:
            source += f" ({EPD_SEMAPHORE_SYMBOL} not in symtab, EPD handle " \
                      f"stays recorded)"
        return cls(addresses, max_priorities, cores, epd, source)


DEFAULT_LAYOUT = KernelLayout.recorded()


def classify_epd_wait(remaining_ticks):
    """Which measured EpdBus wait window can still explain a wake tick?"""
    record = {"remaining_ticks": remaining_ticks, "candidate_branches": [],
              "epd_wait_branch": None, "distinguishable": False, "reason": ""}
    if remaining_ticks is None:
        record["reason"] = "no-wake-tick-measured"
        return record
    if remaining_ticks <= 0:
        record["reason"] = "wake-time-already-reached"
        return record
    candidates = [branch["label"] for branch in EPD_WAIT_BRANCHES
                  if branch["ticks"] >= remaining_ticks]
    record["candidate_branches"] = candidates
    if not candidates:
        record["reason"] = "remaining-exceeds-both-measured-windows"
    elif len(candidates) == 1:
        record["epd_wait_branch"] = candidates[0]
        record["distinguishable"] = True
        record["reason"] = "only-one-measured-window-is-long-enough"
    else:
        record["reason"] = ("both-windows-fit-a-short-remaining-time; a sample "
                            "taken right after the wait starts separates them")
    return record


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

    def __init__(self, qmp, chunk_words=8, layout=None):
        self.qmp = qmp
        self.chunk = chunk_words
        self.layout = layout if layout is not None else DEFAULT_LAYOUT
        self.words = {}
        self.container_labels = {}
        self.walk_report = {}
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
    layout = ram.layout
    if container == 0:
        return "unlinked"
    if container == layout.addr("xSuspendedTaskList"):
        return "suspended-list"
    if container == layout.addr("xTasksWaitingTermination"):
        return "waiting-termination"
    ready = layout.addr("pxReadyTasksLists")
    if ready <= container < ready + layout.max_priorities * LIST_SIZE and \
            (container - ready) % LIST_SIZE == 0:
        return f"ready-priority-{(container - ready) // LIST_SIZE}"
    if container == layout.addr("xDelayedTaskList1"):
        return "delayed-list-1"
    if container == layout.addr("xDelayedTaskList2"):
        return "delayed-list-2"
    pending = layout.addr("xPendingReadyList")
    if pending <= container < pending + layout.cores * LIST_SIZE and \
            (container - pending) % LIST_SIZE == 0:
        return f"pending-ready-core-{(container - pending) // LIST_SIZE}"
    if ram.u32(layout.addr("pxDelayedTaskList")) == container:
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


def decode_wake(ram, tcb_address, x_tick_count=None):
    """When the kernel plans to unblock this task, or why it never will.

    prvAddCurrentTaskToDelayedList writes `xTimeToWake = xConstTickCount +
    xTicksToWait` into xStateListItem.xItemValue (tasks.c:6393) before linking
    the item into a delayed list, so the item value is an absolute wake tick
    only while the item is on one of them. A `portMAX_DELAY` wait takes the
    branch at tasks.c:6381-6386 into xSuspendedTaskList and writes no tick at
    all; reporting a number for that task would be a fabrication.
    """
    item = tcb_address + TCB["x_state_list_item"]
    value = ram.u32(item + LIST_ITEM["x_item_value"])
    if x_tick_count is None:
        x_tick_count = ram.u32(ram.layout.addr("xTickCount"))
    container = ram.u32(item + LIST_ITEM["pv_container"])
    kind = container_kind(ram, container)
    record = {"x_tick_count": x_tick_count, "state_item_x_item_value": _hex(value),
              "state_list": kind, "wake_ticks_remaining": None,
              "wake_tick_meaning": None}
    if value is None or x_tick_count is None:
        record["wake_tick_meaning"] = "unreadable"
        return record
    if kind == "suspended-list":
        record["wake_tick_meaning"] = ("no-wake-tick-indefinite-block-"
                                       "tasks-c-6381-6386")
        return record
    if not (kind or "").startswith("delayed-list"):
        record["wake_tick_meaning"] = ("not-on-a-delayed-list-so-the-item-"
                                       "value-is-not-a-time")
        return record
    difference = (value - x_tick_count) % TICK_SPACE
    if difference >= TICK_SPACE // 2:
        # A delayed item whose wake tick is already behind xTickCount is a
        # stale value, not a wait of four billion ticks.
        record["wake_tick_meaning"] = "wake-tick-already-elapsed-pending-tick-interrupt"
        record["unsigned_difference"] = hex(difference)
        return record
    record["wake_ticks_remaining"] = difference
    record["wake_tick_meaning"] = "absolute-wake-tick-minus-xTickCount"
    record.update(classify_epd_wait(difference))
    return record


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


def scheduler_lists(ram):
    """(label, List_t address) for every list a task can be parked on.

    xSuspendedTaskList and xTasksWaitingTermination are the two the kernel
    itself walks when it enumerates tasks (uxTaskGetSystemState at line 3006
    and line 2998 of components/freertos/FreeRTOS-Kernel/tasks.c). They are
    also where an application task that blocks with `portMAX_DELAY` spends its
    whole life, so a walker that stops at ready/delayed/pending sees IDLE and
    loopTask and nothing else.
    """
    layout = ram.layout
    lists = [(f"ready-priority-{priority}", layout.ready_list(priority))
             for priority in range(layout.max_priorities)]
    lists += [("delayed-list-1", layout.addr("xDelayedTaskList1")),
              ("delayed-list-2", layout.addr("xDelayedTaskList2")),
              ("suspended-list", layout.addr("xSuspendedTaskList")),
              ("waiting-termination",
               layout.addr("xTasksWaitingTermination"))]
    lists += [(f"pending-ready-core-{core}", layout.pending_list(core))
              for core in range(layout.cores)]
    return lists


def walk_list(ram, list_address, seen, label):
    """Follow one List_t to its end, returning rows and a walk report.

    A duplicate or implausible `pvOwner` is skipped, never fatal: the kernel
    legally has the same TCB on one list only, but breaking on the first
    surprise used to hide every task behind it, which is how gap 1 read as
    "application tasks do not exist".
    """
    rows, report = [], {key: 0 for key in WALK_REPORT_BASE}
    end = list_address + LIST["x_list_end"]
    nxt = ram.u32(list_address + MINI_END_NEXT)
    if nxt is None:
        report["read-error"] = True
        return rows, report
    if nxt == 0:
        # A live List_t is always self-linked through xListEnd, even when
        # empty. A null forward pointer means these statics are not a list at
        # all: zeroed RAM, or a build the layout does not describe.
        report["never-initialised"] = True
        return rows, report
    visited = set()
    while nxt is not None and nxt != end and nxt not in visited and \
            report["items_walked"] < MAX_LIST_ITEMS:
        visited.add(nxt)
        report["items_walked"] += 1
        if not _plausible_pointer(nxt):
            report["implausible-item-address"] = hex(nxt)
            break
        owner = ram.u32(nxt + LIST_ITEM["pv_owner"])
        if owner is None:
            report["unreadable-owner"] += 1
        elif not _plausible_pointer(owner):
            report["implausible-owner"] += 1
        elif owner in seen:
            report["duplicate-owner"] += 1
        else:
            seen.add(owner)
            row = task_row(ram, owner)
            if row is None:
                report["undecodable-task"] += 1
            else:
                row["found_in"] = label
                rows.append(row)
        nxt = ram.u32(nxt + LIST_ITEM["px_next"])
    if nxt is not None and nxt != end and report["items_walked"] >= \
            MAX_LIST_ITEMS:
        report["capped-at-max-list-items"] = True
    return rows, report


def enumerate_tasks(ram):
    """Every task on a scheduler list or running on a core. No hardcoded TCB."""
    rows, seen, reports = [], set(), {}
    for label, list_address in scheduler_lists(ram):
        found, report = walk_list(ram, list_address, seen, label)
        rows.extend(found)
        reports[label] = report
        if len(rows) >= MAX_ENUMERATED_TASKS:
            reports["capped-at-max-enumerated-tasks"] = True
            break
    # A running task is on neither a ready nor a delayed list; without this it
    # is invisible to the probe whenever the snapshot catches it on a CPU.
    for core in range(ram.layout.cores):
        tcb = ram.u32(ram.layout.current_tcb(core))
        if not _plausible_pointer(tcb) or tcb in seen:
            continue
        seen.add(tcb)
        row = task_row(ram, tcb)
        if row:
            row["found_in"] = f"running-core-{core}"
            rows.append(row)
        if len(rows) >= MAX_ENUMERATED_TASKS:
            break
    notable = WALK_REPORT_BASE | WALK_QUIET_FLAGS
    ram.walk_report = {}
    for label, report in reports.items():
        if not isinstance(report, dict):
            # Whole-enumeration findings, e.g. the task cap, are never noise.
            ram.walk_report[label] = report
        elif report["items_walked"] or any(key not in notable
                                           for key in report):
            ram.walk_report[label] = report
    return rows


def _hex(value):
    return hex(value) if value is not None else None


def task_matches(name, needle):
    """True if a task name is the requested one.

    The kernel stores at most 16 characters ("ActivityManagerRender" arrives
    as "ActivityManagerR"), so accept either prefix direction.
    """
    return bool(name) and (name == needle[:len(name)] or needle in name)


def snapshot(qmp, task_name_needle="ActivityManagerRender", layout=None):
    """One read-only FreeRTOS + EPD BUSY observation of the running guest."""
    ram = GuestRam(qmp, layout=layout)
    scheduler = ram.u32(ram.layout.addr("xSchedulerRunning"))
    tick = ram.u32(ram.layout.addr("xTickCount"))
    next_unblock = ram.u32(ram.layout.addr("xNextTaskUnblockTime"))
    current = [ram.u32(ram.layout.current_tcb(core))
               for core in range(ram.layout.cores)]
    epd_handle = ram.u32(ram.layout.epd_refresh_done)
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
        "layout": ram.layout.to_dict(),
        "x_scheduler_running": scheduler,
        "x_tick_count": tick,
        "x_next_task_unblock_time": next_unblock,
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
        "task_list_walk": ram.walk_report,
        "render_task": target,
    }
    blocked = analyse_block(ram, target, epd_handle, tick) if target else None
    record["render_task_block"] = blocked
    if record["available"]:
        record["reason"] = f"decoded with layout: {ram.layout.source}"
    else:
        record["reason"] = ("render task not reachable from scheduler lists"
                            if scheduler is not None else
                            "guest RAM read failed; wrong or unmatched build")
        if ram.errors:
            record["read_errors"] = ram.errors[:4]
    return record


def analyse_block(ram, task, epd_handle, x_tick_count=None):
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
        "state_is_suspended": state == "suspended-list",
        "state_is_waiting_termination": state == "waiting-termination",
        "blocked_on_queue": queue_address is not None,
        "blocked_queue": queue,
        "blocked_queue_matches_epd_refresh_done": bool(
            queue_address and epd_handle and
            queue_address == epd_handle),
        "holding_mutexes": task["ux_mutexes_held"],
        "notification_value": task["ul_notified_value"],
        "notify_state": task["uc_notify_state"],
        "wake": decode_wake(ram, int(task["tcb"], 16), x_tick_count),
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
    pre_wake, post_wake = pre.get("wake") or {}, post.get("wake") or {}
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
        "render_task_state_list_before": pre.get("state_list"),
        "render_task_state_list_after": post.get("state_list"),
        "left_indefinite_block": bool(
            ok and pre.get("state_is_suspended") and
            not post.get("state_is_suspended")),
        "still_parked_in_delayed_list": bool(
            ok and post.get("state_is_delayed")),
        "still_parked_indefinitely": bool(ok and post.get("state_is_suspended")),
        "x_tick_count_before": before.get("x_tick_count"),
        "x_tick_count_after": after.get("x_tick_count"),
        "wake_ticks_remaining_before": pre_wake.get("wake_ticks_remaining"),
        "wake_ticks_remaining_after": post_wake.get("wake_ticks_remaining"),
        "wake_tick_meaning_before": pre_wake.get("wake_tick_meaning"),
        "wake_tick_meaning_after": post_wake.get("wake_tick_meaning"),
        "epd_wait_branch_after": post_wake.get("epd_wait_branch"),
        "epd_wait_candidates_after": post_wake.get("candidate_branches", []),
    }
