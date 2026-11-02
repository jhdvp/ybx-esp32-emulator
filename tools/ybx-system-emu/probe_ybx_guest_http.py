#!/usr/bin/env python3
"""Read-only health check for an isolated QEMU 11 CrossPoint instance.

Checks the preview bridge and firmware HTTP separately. This script never
requests token.json, sends input events, changes the guest SD or prints bodies.
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import struct
import subprocess
import sys
from urllib.request import urlopen

GUEST_CHECK = """
import http.client, json, sys
conn = http.client.HTTPConnection("127.0.0.1", int(sys.argv[1]), timeout=5)
try:
    conn.request("GET", sys.argv[2], headers={"Accept": "application/json"})
    response = conn.getresponse()
    print(json.dumps({"status": response.status}))
except Exception as exc:
    print(json.dumps({"error_type": type(exc).__name__}))
finally:
    conn.close()
"""


def bridge_probe(port: int) -> dict:
    result: dict = {"reachable": False}
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/state", timeout=7) as r:
            state = json.load(r)
        result["reachable"] = True
        result["qmp_running"] = state.get("qmp_status") == "running"
        result["firmware_boot_observed"] = bool(state.get("home_log_observed"))
        # A running QMP process is NOT evidence that the Wi-Fi or HTTP works.
        with urlopen(f"http://127.0.0.1:{port}/screen.png", timeout=8) as r:
            header = r.read(24)
        if header[:8] == bytes.fromhex("89504e470d0a1a0a") and len(header) == 24:
            result["screen_dimensions"] = list(struct.unpack(">II", header[16:24]))
        else:
            result["screen_error"] = "invalid_png_header"
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    return result


def guest_probe(container: str, port: int, path: str) -> dict:
    try:
        proc = subprocess.run(
            ["docker", "exec", container, "python3", "-c",
             GUEST_CHECK, str(port), path],
            capture_output=True, text=True, timeout=12, check=False,
        )
        if proc.returncode:
            return {"error_type": "docker_exec_failed", "exit_code": proc.returncode}
        return json.loads(proc.stdout.strip())
    except subprocess.TimeoutExpired:
        return {"error_type": "docker_exec_timeout"}
    except (OSError, ValueError):
        return {"error_type": "docker_probe_unavailable"}


def run(bridge_port: int, container: str, guest_port: int) -> dict:
    bridge = bridge_probe(bridge_port)
    result = {"bridge": bridge, "guest_plugins": {"error_type": "not_checked"},
              "guest_plugin_js": {"error_type": "not_checked"}}
    if bridge.get("qmp_running"):
        result["guest_plugins"] = guest_probe(container, guest_port, "/api/plugins")
        if result["guest_plugins"].get("status") == 200:
            result["guest_plugin_js"] = guest_probe(
                container, guest_port, "/plugin?name=ybx&file=plugin.js"
            )
    result["ok"] = (
        bridge.get("qmp_running") is True
        and bridge.get("screen_dimensions") == [480, 800]
        and result["guest_plugins"].get("status") == 200
        and result["guest_plugin_js"].get("status") == 200
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-port", type=int, default=8820)
    parser.add_argument("--guest-port", type=int, default=18080)
    parser.add_argument("--container", default="ybx-eink-qemu11-8820")
    args = parser.parse_args()
    if not (8800 <= args.bridge_port <= 65535):
        parser.error("bridge must be on a separate high-numbered loopback port")
    if not (1024 <= args.guest_port <= 65535):
        parser.error("guest HTTP hostfwd port must be nonprivileged")
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", args.container):
        parser.error("invalid Docker container name")
    result = run(args.bridge_port, args.container, args.guest_port)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
