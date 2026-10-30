#!/usr/bin/env python3
"""Independent check for experiment 0037 (host-only, no QEMU/Docker).

Two checks:
 1. waitBusy-branch model: replicate the bounds of the three EpdBus::waitBusy
    poll branches (EpdBus.cpp:306-377) against a BUSY pin stuck LOW and show
    that only BusyPolarity::UcIdleHigh loops forever.
    Branch facts encoded here (verified by reading the symbols checkout):
      - ActiveHigh : while(read==HIGH){...; if(millis-start>30000) break}  -> idles when LOW
      - ActiveLow  : 100ms grace + while(LOW){...; if(>30000) break}       -> breaks at 30s
      - UcIdleHigh : delay(1); while(read==LOW){busyIdle(1)}               -> NO timeout
      - X3TwoPhase : 1s edge wait + while(LOW){...; if(>30000) break}      -> breaks at 30s
 2. QA JSON evidence: print before/after frame counters, RAM write counts,
    frame SHAs and GPIO6 (bit6 of GPIO_IN_REG 0x6000403C sample) from the
    0041/0042 telemetry dumps, and assert the 0037 claims:
      - 0041: counters frozen before==after
      - 0042: after-frames > before-frames AND after SHA != before SHA
      - 0042: GPIO6 input bit == 0 (BUSY reads LOW under UcIdleHigh = busy)

Usage: python3 0037-render-busy-poll-check.py [path-to-qa-dir]
Exit 0 = model result as predicted and (if JSONs present) evidence asserts pass.
"""
import json
import sys
from pathlib import Path

DEFAULT_QA = Path(
    "/Users/yaraslauhancharou/Projects/YBX/artifacts/"
    "ybx-esp32-system-emulation-20261029/qa-20261029"
)


class VirtualClock:
    def __init__(self):
        self.ms = 0

    def millis(self):
        return self.ms


def poll_wait_busy(polarity, read_level_fn, clock, tag=""):
    """Return (result, elapsed_ms). result: 'idle' | 'bail30s' | 'HANG'.
    HANG is detected by a generous observation cap far above 30s."""
    start = clock.millis()
    OBSERVE_CAP_MS = 120_000  # only the untimed branch can reach this
    iterations = 0
    while True:
        iterations += 1
        level = read_level_fn()          # 'HIGH' | 'LOW'
        elapsed = clock.millis() - start
        if elapsed > OBSERVE_CAP_MS:
            return "HANG", elapsed
        if polarity == "ActiveHigh":
            if level != "HIGH":
                return "idle", elapsed
        elif polarity == "ActiveLow":
            if level == "HIGH" and elapsed >= 100:
                return "idle", elapsed
        elif polarity == "UcIdleHigh":
            if level != "LOW":
                return "idle", elapsed
        elif polarity == "X3TwoPhase":
            # Phase 1: wait for the LOW edge (<=1s); already LOW => go to phase 2.
            # Phase 2: wait back to HIGH with the 30s bail (handled below).
            pass
        if elapsed > 30_000 and polarity in ("ActiveLow", "X3TwoPhase", "ActiveHigh"):
            return "bail30s", elapsed
        clock.ms += 1  # delay(1) per busyIdle fallback step
    # unreachable


def check_model():
    print("== 1. waitBusy branch model vs BUSY stuck LOW ==")
    ok = True
    cases = [
        ("ActiveHigh", "idle"),      # LOW is already 'not busy' for this polarity
        ("ActiveLow", "bail30s"),
        ("UcIdleHigh", "HANG"),
        ("X3TwoPhase", "bail30s"),
    ]
    for polarity, expected in cases:
        clock = VirtualClock()
        result, elapsed = poll_wait_busy(polarity, lambda: "LOW", clock)
        mark = "OK " if result == expected else "FAIL"
        if result != expected:
            ok = False
        print(f"  [{mark}] {polarity:12s} stuck-LOW -> {result} ({elapsed} ms), expected {expected}")
    print("  => only BusyPolarity::UcIdleHigh (EpdBus.cpp:342-358) can hang forever;")
    print("     UcIdleHigh is the polarity of Uc8179Driver.h:64 / Uc8279X4Driver.h:69.")
    return ok


def get(d, *keys):
    for k in keys:
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return None
    return d


def check_json(qa: Path):
    print(f"\n== 2. QA telemetry evidence ({qa}) ==")
    if not qa.is_dir():
        print("  [SKIP] directory not found")
        return True
    ok = True

    f41 = qa / "s3-x4pro-epd-isr-semaphore-0041.json"
    if f41.exists():
        data = json.loads(f41.read_text())
        frames = [get(data, s, "frames") for s in ("before", "during", "after")]
        shas = {get(data, s, "frame_sha256") for s in ("before", "during", "after")}
        print(f"  0041 frames before/during/after: {frames}; distinct SHAs: {len(shas)}")
        if len(set(frames)) == 1 and len(shas) == 1:
            print("  [OK ] 0041 counters frozen (frames + SHA identical, old QEMU build)")
        else:
            ok = False
            print("  [FAIL] expected 0041 frozen")

    f42 = qa / "s3-x4pro-gpio6-pcpu-irq-0042-2s-final.json"
    if f42.exists():
        data = json.loads(f42.read_text())
        bf = get(data, "before", "frames")
        af = get(data, "after", "frames")
        bs = get(data, "before", "frame_sha256")
        as_ = get(data, "after", "frame_sha256")
        print(f"  0042 frames {bf} -> {af}; sha changed: {bs != as_} ({str(as_)[:16]}…); "
              f"flags: sha_changed={data.get('guest_epd_frame_sha_changed_after_contact')}, "
              f"count_increased={data.get('guest_epd_frame_count_increased')}")
        if bf is not None and af is not None and af > bf and bs != as_:
            print("  [OK ] 0042: navigation produced a new frame + RAM SHA change (new QEMU build 4106cff2…)")
        else:
            ok = False
            print("  [FAIL] 0042 progress assertion")
        raw = get(data, "cpu_after", "gpio_status_mmio", "input-lo")
        if isinstance(raw, str) and ":" in raw:
            in_lo = int(raw.split(":")[1].strip(), 16)
            bit6 = (in_lo >> 6) & 1
            print(f"  0042 GPIO_IN_REG 0x6000403C = 0x{in_lo:08x}, bit6 (BUSY GPIO6) = {bit6}")
            if bit6 == 0:
                print("  [OK ] BUSY GPIO6 reads LOW => 'permanently busy' under UcIdleHigh polarity")
            else:
                ok = False
                print("  [FAIL] expected BUSY bit6=0")
        else:
            print("  [SKIP] cpu_after.gpio_status_mmio.input-lo not found in 0042 JSON")
    else:
        print(f"  [SKIP] missing {f41.name if not f41.exists() else ''}{f42.name}")
    return ok


def main():
    qa = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_QA
    ok = check_model() and check_json(qa)
    print("\nRESULT:", "PASS" if ok else "ATTENTION (see FAIL/WARN lines)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
