#!/usr/bin/env python3
"""Probe guest HTTP through QEMU11 Wi-Fi user-NAT, strictly container-local.

Run with docker exec INSIDE an isolated YBX eink-qemu11 preview container.
The temporary hostfwd binds 127.0.0.1 within that container only.
"""
import argparse
from contextlib import closing
import glob
import json
import socket
import time
import urllib.request


def qmp_open(path):
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(6)
    sock.connect(path)
    reader = sock.makefile("rb")
    assert "QMP" in json.loads(reader.readline())
    def cmd(name, opts=None):
        data = {"execute": name}
        if opts is not None:
            data["arguments"] = opts
        sock.sendall((json.dumps(data) + "\n").encode())
        for _ in range(50):
            response = json.loads(reader.readline())
            if "event" in response:
                continue
            if "error" in response:
                raise RuntimeError(f"{name}: {response['error']}")
            return response["return"]
        raise RuntimeError(f"QMP {name}: no reply")
    cmd("qmp_capabilities")
    return sock, reader, cmd


def run(port):
    if not (1024 <= port < 65536):
        raise ValueError("invalid container-local port")
    sock_files = glob.glob("/tmp/ybx-einkqemu11-*/qmp.sock")
    if len(sock_files) != 1:
        raise RuntimeError(f"expected exactly one QEMU11 runtime socket, got {len(sock_files)}")
    sock, reader, cmd = qmp_open(sock_files[0])
    rule = f"tcp:127.0.0.1:{port}-:80"
    summary = {"probe": "QEMU11 guest HTTP over ESP32 Wi-Fi MAC + user NAT",
               "forward_rule": rule, "scope": "within isolated Docker container"}
    try:
        summary["network"] = cmd("human-monitor-command",
                                 {"command-line": "info network"})[-1800:]
        added = cmd("human-monitor-command",
                    {"command-line": f"hostfwd_add {rule}"})
        summary["forward_add"] = added
        if "error" in str(added).lower() or "unknown" in str(added).lower():
            raise RuntimeError(f"QEMU did not accept hostfwd: {added}")
        try:
            for attempt in range(1, 7):
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/", timeout=5
                    ) as resp:
                        body = resp.read(4096)
                        summary.update({"http_status": resp.status,
                                        "body_bytes": len(body),
                                        "content_type": resp.headers.get("Content-Type", ""),
                                        "body_start": body[:160].decode(errors="replace"),
                                        "attempts": attempt})
                        break
                except Exception as exc:
                    summary["last_error"] = str(exc)
                    time.sleep(1)
            summary["http_pass"] = summary.get("http_status") == 200
        finally:
            summary["forward_remove"] = cmd("human-monitor-command",
                {"command-line": f"hostfwd_remove tcp:127.0.0.1:{port}"})
        if "removed" not in summary["forward_remove"].lower():
            raise RuntimeError("hostfwd_remove failed: " + summary["forward_remove"])
        summary["forward_removed"] = True
    finally:
        reader.close()
        sock.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--forward-port", type=int, default=18080)
    args = p.parse_args()
    report = run(args.forward_port)
    raise SystemExit(0 if report.get("http_pass") else 1)
