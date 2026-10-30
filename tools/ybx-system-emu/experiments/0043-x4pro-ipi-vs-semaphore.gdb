# Read-only QEMU Xtensa GDB discriminator for ORIGINAL 092c2be5 X4 Pro factory ELF.
# Question: after the GT911 "More" tap, is the render task stuck because QEMU lost a
# cross-core IPI, or because it is blocked on an unrelated synchronisation object?
#
# Run from the matching firmware worktree (never production firmware):
#   xtensa-esp32s3-elf-gdb -batch -nx -x tools/ybx-system-emu/experiments/0043-x4pro-ipi-vs-semaphore.gdb
# QEMU must be started with -S -gdb tcp:0.0.0.0:25989 and only localhost 25989 forwarded.
# Inject the GT911 More press+release only after a stable 2-frame original Home screen.
#
# This script only reads guest memory and MMIO and sets breakpoints. It never writes
# memory, registers, or interrupt state, so it cannot fake an interrupt delivery.
#
# Addresses are from the same factory ELF:
#   0x40384ffc vPortYieldOtherCore(BaseType_t xCoreID)          remote-yield request, per core
#   0x4037675c esp_crosscore_int_send_yield(int core_id)        IDF yield IPI trigger
#   0x40376710 esp_crosscore_int_send(int core_id, reason_mask) all FROM_CPU reasons
#   0x403766b4 esp_crosscore_isr()                              target CPU took the IPI
#   0x403845e0 xQueueGiveFromISR()                              semaphore given from an ISR
#   0x4210225d / 0x42102260 ActivityManager::loop around xTaskGenericNotify (see 0036)
# Xtensa windowed ABI: before the callee's entry, arguments sit in a2..a5, so a2 is the
# first parameter of each function above.
# Kernel type names were checked against this ELF's DWARF: the list container member is
# pvContainer here (not pxContainer), and &xDelayedTaskList1 links at 0x3fcac00c.
set pagination off
set confirm off
set remotetimeout 8
file .pio/build/x4pro/firmware.elf
target remote 127.0.0.1:25989

# --- 1. Notification site: arm the IPI probes only around the notify call. ---
break *0x4210225d
commands 1
silent
printf "NOTIFY_PRE handle=0x%x\n", $a10
set $renderh = $a10
enable 2
enable 3
enable 4
disable 1
continue
end

# --- 2. Kernel asked for a remote wake? Any hit below disproves "no IPI requested". ---
# Created disabled: boot-time cross-core traffic would stop the guest thousands of
# times. Breakpoint 1 arms them for the notify call window only.
break *0x40384ffc
disable 2
commands 2
silent
printf "IPI_REQ_YIELD_CORE core=%d\n", $a2
continue
end

break *0x4037675c
disable 3
commands 3
silent
printf "IPI_REQ_SEND_YIELD core=%d\n", $a2
continue
end

break *0x40376710
disable 4
commands 4
silent
printf "IPI_REQ_SEND core=%d reason=0x%x\n", $a2, $a3
continue
end

# --- 3. Notify returned: dump kernel state, then look at the delivery side. ---
break *0x42102260
commands 5
silent
disable 2
disable 3
disable 4
printf "NOTIFY_POST result=0x%x\n", $a10
# TCB facts. If the cast does not resolve in this frame, break inside
# xTaskGenericNotify (0x40387444) where the current unit is tasks.c and re-run the
# same expressions.
printf "TCB_NAME=%s\n", ((struct tskTaskControlBlock *)$renderh)->pcTaskName
printf "TCB_PRIORITY=%u\n", ((struct tskTaskControlBlock *)$renderh)->uxPriority
printf "TCB_COREID=%d\n", ((struct tskTaskControlBlock *)$renderh)->xCoreID
printf "TCB_NOTIFIED_VALUE=%u\n", ((struct tskTaskControlBlock *)$renderh)->ulNotifiedValue[0]
printf "TCB_NOTIFY_STATE=%u\n", ((struct tskTaskControlBlock *)$renderh)->ucNotifyState[0]
# Where the task list items live right now: delayed/ready/suspended/pending.
printf "TCB_STATE_CONTAINER=0x%x\n", ((struct tskTaskControlBlock *)$renderh)->xStateListItem.pvContainer
printf "TCB_EVENT_CONTAINER=0x%x\n", ((struct tskTaskControlBlock *)$renderh)->xEventListItem.pvContainer
# Reference list addresses (statically linked, stable across runs). The ready list for
# this task's own priority is named explicitly so no sizeof(List_t) arithmetic is needed.
printf "ADDR_DELAYED1=0x%x\n", &xDelayedTaskList1
printf "ADDR_READY_AT_PRIO=0x%x\n", &pxReadyTasksLists[((struct tskTaskControlBlock *)$renderh)->uxPriority]
printf "READY_AT_PRIO_ITEMS=%u\n", pxReadyTasksLists[((struct tskTaskControlBlock *)$renderh)->uxPriority].uxNumberOfItems
printf "ADDR_PENDING_READY_0=0x%x\n", &xPendingReadyList[0]
printf "ADDR_PENDING_READY_1=0x%x\n", &xPendingReadyList[1]
printf "PENDING_READY_0=%u\n", xPendingReadyList[0].uxNumberOfItems
printf "PENDING_READY_1=%u\n", xPendingReadyList[1].uxNumberOfItems
printf "SCHED_SUSPENDED_0=%u\n", uxSchedulerSuspended[0]
printf "SCHED_SUSPENDED_1=%u\n", uxSchedulerSuspended[1]
printf "RUNNING_0=%s\n", pxCurrentTCBs[0]->pcTaskName
printf "RUNNING_1=%s\n", pxCurrentTCBs[1]->pcTaskName
# The object the event list item points at: a semaphore/queue, not a notification.
printf "SEM_HANDLE=0x%x\n", freeink::s_epdRefreshDone
printf "SEM_QUEUE_TYPE=%u\n", ((struct QueueDefinition *)freeink::s_epdRefreshDone)->ucQueueType
printf "SEM_LENGTH=%u\n", ((struct QueueDefinition *)freeink::s_epdRefreshDone)->uxLength
printf "SEM_ITEM_SIZE=%u\n", ((struct QueueDefinition *)freeink::s_epdRefreshDone)->uxItemSize
printf "SEM_MESSAGES=%u\n", ((struct QueueDefinition *)freeink::s_epdRefreshDone)->uxMessagesWaiting
printf "SEM_WAIT_LIST=0x%x\n", &((struct QueueDefinition *)freeink::s_epdRefreshDone)->xTasksWaitingToReceive
printf "SEM_WAITERS=%u\n", ((struct QueueDefinition *)freeink::s_epdRefreshDone)->xTasksWaitingToReceive.uxNumberOfItems
# Emulator-visible hardware state of the cross-core path, side-effect free reads:
# SYSTEM_CPU_INTR_FROM_CPU_0..3 (0x600C0030..3C) latch the request level, and
# INTR_MATRIX core0/core1 entries for sources 79..82 (0x600C213C..140, 0x600C293C..940)
# hold the CPU interrupt number each core routes FROM_CPU to.
printf "FROM_CPU_LEVELS=%u %u %u %u\n", *(unsigned int *)0x600c0030, *(unsigned int *)0x600c0034, *(unsigned int *)0x600c0038, *(unsigned int *)0x600c003c
printf "FROM_CPU_MAP_CORE0=%u %u %u %u\n", *(unsigned int *)0x600c213c, *(unsigned int *)0x600c2140, *(unsigned int *)0x600c2144, *(unsigned int *)0x600c2148
printf "FROM_CPU_MAP_CORE1=%u %u %u %u\n", *(unsigned int *)0x600c293c, *(unsigned int *)0x600c2940, *(unsigned int *)0x600c2944, *(unsigned int *)0x600c2948
x/4wx 0x600c0030
x/4wx 0x600c213c
x/4wx 0x600c293c
enable 6
enable 7
disable 5
continue
end

# --- 4. Delivery side during the stall window: did the target CPU ever run the ISR? ---
break *0x403766b4
disable 6
commands 6
silent
printf "IPI_TAKEN_ISR\n"
continue
end

# --- 5. Was the blocking object ever given? (EPD BUSY completion path) ---
# Any semaphore given from any ISR prints its handle; compare with SEM_HANDLE.
break *0x403845e0
disable 7
commands 7
silent
printf "SEMAPHORE_GIVE_FROM_ISR obj=0x%x\n", $a2
continue
end

continue
