#!/usr/bin/env python3
"""Press native X4 Pro side buttons on an isolated eink-qemu 11 guest via QMP.

GT911 swipes are not emulated reliably; menus and pages move with the up/down GPIO keys.
    python3 stock1614-key.py down        # one press
    python3 stock1614-key.py down 3      # three presses
    python3 stock1614-key.py up --qmp 8972
"""
import argparse
import json
import socket
import time

RESERVED = {8951, 8965}  # QMP of other runs' containers


def qmp_call(port, execute, arguments=None):
    with socket.create_connection(("127.0.0.1", port), timeout=8) as sock:
        f = sock.makefile("rw")

        def read():
            while True:
                msg = json.loads(f.readline())
                if "event" not in msg:
                    return msg

        read()
        f.write(json.dumps({"execute": "qmp_capabilities"}) + "\n"); f.flush(); read()
        cmd = {"execute": execute}
        if arguments:
            cmd["arguments"] = arguments
        f.write(json.dumps(cmd) + "\n"); f.flush()
        reply = read()
        if "error" in reply:
            raise SystemExit("QMP error: %s" % reply["error"])
        return reply.get("return")


def press(port, key, hold=0.05):
    path = "/machine/x4pro-keys"
    qmp_call(port, "qom-set", {"path": path, "property": key, "value": True})
    try:
        time.sleep(hold)
    finally:
        qmp_call(port, "qom-set", {"path": path, "property": key, "value": False})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("key", choices=["up", "down"])
    ap.add_argument("count", nargs="?", type=int, default=1)
    ap.add_argument("--qmp", type=int, default=8972, help="QMP host port of the canary container (default 8972)")
    ap.add_argument("--gap", type=float, default=0.6, help="seconds between presses (e-ink refresh)")
    args = ap.parse_args()
    if args.qmp in RESERVED:
        raise SystemExit("port %d belongs to another run's container" % args.qmp)
    for i in range(max(1, min(args.count, 50))):
        press(args.qmp, args.key)
        if i + 1 < args.count:
            time.sleep(args.gap)
    print("pressed %s x%d" % (args.key, args.count))


if __name__ == "__main__":
    main()
