# 0037-freertos-queue-tcb-probe.gdb
# Read-only forensic probe for the X4 Pro 092c2be5 factory build.
# Reverse-maps Queue_t 0x3fcec294 (freeink::s_epdRefreshDone binary semaphore),
# the ActivityManagerRender TCB 0x3fcec420, and the rendering mutex 0x3fcbc460.
#
# Attach form (guest may be running; every command here only reads memory):
#   xtensa-esp32s3-elf-gdb -batch -q .pio/build/x4pro/firmware.elf \
#     -ex "target remote :<qemu-gdb-port>" -x 0037-freertos-queue-tcb-probe.gdb
#
# Field offsets were taken from this exact ELF's DWARF (see
# test_0037_freertos_queue_mapping.py): Queue_t.xTasksWaitingToReceive=+0x24,
# TCB size=352, xEventListItem=+0x18 (pvContainer=+0x28),
# xStateListItem=+0x04 (pvContainer=+0x14, xItemValue=+0x04),
# ulNotifiedValue=+0x158, ucNotifyState=+0x15C, pxTopOfStack=+0x00,
# XtExcFrame: pc=+0x04, a0=+0x0C.
#
# Recorded evidence being re-derived (0036/0037b/0041/0042):
#   0x3fcec294 + 0x24 = 0x3fcec2b8  (waiter xEventListItem.pvContainer)
#   s_epdRefreshDone global 0x3fca1da8 holds 0x3fcec294
#   xDelayedTaskList1 header 0x3fcac00c, xDelayedTaskList2 0x3fcabff8
#   EpdBus::waitRefreshComplete take sites:
#     0x4201e9e5 drain timeout 0   (EpdBus.cpp:437, cannot block)
#     0x4201ea15 20 ms take        (EpdBus.cpp:445, return 0x4201ea18)
#     0x4201ea6e 30000-tick take   (EpdBus.cpp:460, return 0x4201ea71)

set pagination off
set confirm off

set $Q_BASE   = 0x3fcec294
set $RENDER   = 0x3fcec420
set $MTX_BASE = 0x3fcbc460
set $SEM_GLOB = 0x3fca1da8
set $TICK_ADDR      = 0x3fcabf94
set $NXTUNBLK_ADDR  = 0x3fcabf74

set $sem = (struct QueueDefinition *)$Q_BASE
set $mtx = (struct QueueDefinition *)$MTX_BASE
set $r   = (struct tskTaskControlBlock *)$RENDER

# 1. Identity: the blocked queue must equal the value of freeink::s_epdRefreshDone
printf "P1 sem=%p s_epdRefreshDone=%p match=%d\n", $sem, *(void **)$SEM_GLOB, (*(unsigned int *)$SEM_GLOB == $Q_BASE)

# 2. Semaphore object fields (type 3 = queueQUEUE_TYPE_BINARY_SEMAPHORE)
printf "P2 sem uxLength=%u uxItemSize=%u uxMessagesWaiting=%u ucQueueType=%u\n", \
  $sem->uxLength, $sem->uxItemSize, $sem->uxMessagesWaiting, $sem->ucQueueType
printf "P2 &xTasksWaitingToReceive=%p expect=0x3fcec2b8\n", &($sem->xTasksWaitingToReceive)

# 3. Walk xTasksWaitingToReceive (all takers of this semaphore)
set $hdr = &($sem->xTasksWaitingToReceive)
set $end = (struct xLIST_ITEM *)&($hdr->xListEnd)
set $it  = (struct xLIST_ITEM *)$hdr->xListEnd.pxNext
set $n   = 0
while $it != $end && $n < 16
  set $t = (struct tskTaskControlBlock *)$it->pvOwner
  printf "P3 waiter[%d] tcb=%p name=%.16s eventItemVal=%u stateItemVal(wake)=%u core=%d\n", \
    $n, $t, $t->pcTaskName, $it->xItemValue, $t->xStateListItem.xItemValue, $t->xCoreID
  set $it = $it->pxNext
  set $n = $n + 1
end

# 4. Render TCB: list membership + notification state
printf "P4 render name=%.16s prio=%u base=%u core=%d mutexesHeld=%u\n", \
  $r->pcTaskName, $r->uxPriority, $r->uxBasePriority, $r->xCoreID, $r->uxMutexesHeld
printf "P4 render ulNotifiedValue=%u ucNotifyState=%u (0=notWaiting 1=waitingForNotify 2=received)\n", \
  $r->ulNotifiedValue[0], $r->ucNotifyState[0]
printf "P4 render stateContainer=%p isDelayed1=%d isDelayed2=%d\n", \
  $r->xStateListItem.pvContainer, \
  (void *)$r->xStateListItem.pvContainer == (void *)&xDelayedTaskList1, \
  (void *)$r->xStateListItem.pvContainer == (void *)&xDelayedTaskList2
printf "P4 render eventContainer=%p isQ_recv=%d\n", \
  $r->xEventListItem.pvContainer, \
  (void *)$r->xEventListItem.pvContainer == (void *)&($sem->xTasksWaitingToReceive)
set $tick = *(unsigned int *)$TICK_ADDR
set $wake = $r->xStateListItem.xItemValue
printf "P4 render wakeTick=%u xTickCount=%u remaining=%u (~30000=>EpdBus.cpp:460 @0x4201ea6e; ~20=>:445 @0x4201ea15)\n", \
  $wake, $tick, $wake - $tick

# 5. Stacked context of the blocked render task.
# A timed queue-receive blocks in queue.c (vTaskPlaceOnEventList, tasks.c:3767;
# portYIELD_WITHIN_API, queue.c:1831); the tick ISR that performs the actual
# switch saves an XtExcFrame at pxTopOfStack: savedPC at +0x04, window A0 at +0x0C.
set $sp = (unsigned int *)$r->pxTopOfStack
printf "P5 frame sp=%p savedPC=0x%x savedA0=0x%x\n", $sp, $sp[1], $sp[3]
info symbol $sp[1]
info symbol $sp[3]
# Manual caller-chain scan: run `info symbol 0x<word>` on words below that fall
# in 0x40000000-0x48000000 (IRAM/flash text). Expected: xQueueSemaphoreTake
# (0x4038473c..0x40384844) and EpdBus::waitRefreshComplete return sites
# 0x4201ea71 (30 s loop) or 0x4201ea18 (20 ms wait).
x/48xw $sp

# 6. renderingMutex 0x3fcbc460: type + recorded holder (mutex types only)
printf "P6 mtx uxLength=%u uxItemSize=%u uxMessagesWaiting=%u ucQueueType=%u holder=%p\n", \
  $mtx->uxLength, $mtx->uxItemSize, $mtx->uxMessagesWaiting, $mtx->ucQueueType, \
  $mtx->u.xSemaphore.xMutexHolder
set $h = (struct tskTaskControlBlock *)$mtx->u.xSemaphore.xMutexHolder
printf "P6 holder name=%.16s prio=%u core=%d\n", $h->pcTaskName, $h->uxPriority, $h->xCoreID

# 7. Scheduler globals
printf "P7 xTickCount=%u xNextTaskUnblockTime=%u delayed1Items=%u delayed2Items=%u\n", \
  *(unsigned int *)$TICK_ADDR, *(unsigned int *)$NXTUNBLK_ADDR, \
  xDelayedTaskList1.uxNumberOfItems, xDelayedTaskList2.uxNumberOfItems

# 8. GPIO6 BUSY wiring evidence (guest MMIO; QEMU gdbstub may refuse these —
# HMP `xp /1wx 0x6000408c` was the working path in 0037b).
# GPIO_PIN6_REG = 0x6000408C: int_type bits[9:7], int_ena bits[17:13].
# EpdBus.cpp arms attachInterrupt(pin, epdBusyIsr, CHANGE) at 0x4201e9fe right
# before the 20 ms/30 s takes, so a task blocked on s_epdRefreshDone with
# int_ena==0 means the write never reached the modeled register file.
printf "P8 GPIO6 pin_cfg=0x%x int_type=%u int_ena=0x%x\n", \
  *(unsigned int *)0x6000408c, \
  ((*(unsigned int *)0x6000408c) >> 7) & 7, \
  ((*(unsigned int *)0x6000408c) >> 13) & 0x1f
printf "P8 GPIO_IN=0x%x bit6=%d STATUS=0x%x\n", \
  *(unsigned int *)0x6000403c, \
  ((*(unsigned int *)0x6000403c) >> 6) & 1, \
  *(unsigned int *)0x60004044
