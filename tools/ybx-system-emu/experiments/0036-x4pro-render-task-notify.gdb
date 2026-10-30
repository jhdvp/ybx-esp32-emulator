# Read-only QEMU Xtensa GDB probes for ORIGINAL 092c2be5 X4 Pro factory ELF.
# Run from the matching firmware worktree, not production firmware:
# xtensa-esp32s3-elf-gdb -batch -nx -x tools/ybx-system-emu/experiments/0036-x4pro-render-task-notify.gdb
# Run QEMU with -S -gdb tcp:0.0.0.0:25989 and forward ONLY localhost 25989.
# The accompanying guest probe must inject GT911 More press+release after
# a stable 2-frame original home screen. Do not treat GDB as panel validation.
set pagination off
set confirm off
set remotetimeout 8
file .pio/build/x4pro/firmware.elf
target remote 127.0.0.1:25989

break HomeActivity::onMoreOpen
commands 1
silent
printf "ORIGINAL_GUEST_MORE_OPEN_ENTERED\n"
enable 2
disable 1
continue
end

# ActivityManager::loop about to invoke xTaskGenericNotify.
break *0x4210225d
disable 2
commands 2
silent
printf "RENDER_TASK_HANDLE="
print/x $a10
enable 3
disable 2
continue
end

# After FreeRTOS xTaskGenericNotify, a10 is the function's return value.
break *0x42102260
disable 3
commands 3
silent
printf "X_TASK_GENERIC_NOTIFY_RESULT="
print/x $a10
enable 4
disable 3
continue
end

# Only enabled after More's notification. Hit = render task woke.
break *0x421018f4
disable 4
commands 4
silent
printf "RENDER_TASK_WOKE_FROM_UL_TASK_NOTIFY_TAKE\n"
enable 5
disable 4
continue
end

break HomeActivity::render
disable 5
commands 5
silent
printf "HOME_RENDER_AFTER_MORE_NOTIFICATION\n"
disable 5
continue
end
continue
