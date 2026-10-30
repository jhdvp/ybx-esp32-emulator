# Read-only debugger checkpoints for unmodified X4 Pro factory firmware.
# Launch with the matching x4pro/firmware.elf as the GDB positional argument:
# xtensa-esp32s3-elf-gdb -batch -nx <firmware.elf> -x <this file>
# QEMU must have -gdb tcp:0.0.0.0:25997. Attach after HOME is ready.
# Note: GDB inspection can alter elapsed virtual timing; never use this
# script alone to claim an end-to-end touch-to-panel success.
set pagination off
set confirm off
set print pretty on
set remotetimeout 8
target remote 127.0.0.1:25997

# This line executes AFTER 'self.pending = event.value'.
# Values printed at the onAction FUNCTION ENTRY may be ABI/DWARF-invalid.
break src/components/YbxHomeUi.cpp:43
commands
  silent
  printf "\nYBX_ON_ACTION_AFTER_PENDING_ASSIGNMENT\n"
  print self.pending
  print event.action
  print event.value
  disable 1
  continue
end

# routeTouch(input) completed at the return line. A tap must both carry
# touchReleased with logical (350,760) and set pending to the hit action.
break src/components/YbxHomeUi.cpp:49 if touch.snap.touchReleased
commands
  silent
  printf "\nYBX_SELECTED_ACTION_AFTER_ROUTE\n"
  print pending
  print touch.snap.touchReleased
  print touch.snap.touchX
  print touch.snap.touchY
  disable 2
  continue
end

# This breakpoint fires only if HomeActivity got a nonnegative selection.
# If it never fires, inspect pending above and the valid index range.
break src/activities/home/HomeActivity.cpp:418 if touched >= 0
commands
  silent
  printf "\nYBX_HOME_ACTIVITY_VALID_SELECTION\n"
  print touched
  print recentBooks.size()
  disable 3
  continue
end

# Optional final acceptance checkpoint: the firmware must actually enter
# action execution, but QEMU must STILL show a changed SSD1677 frame
# independently before reporting successful end-to-end touch/UI behavior.
break src/activities/home/HomeActivity.cpp:421
commands
  silent
  printf "\nYBX_HOME_ACTIVITY_ACTIVATE_SELECTION\n"
  print selectorIndex
  disable 4
  continue
end
continue
