# 0091 — sleep-acceptance suite: withdrawn

Date: 2026-10-31 (lane) → 2026-11-01 (orchestrator decision).
Lane: batch-5 `sleep-acceptance` (chained after `sleep-wake`).

This lane extracted the sleep/wake acceptance into
`test_x4pro_sleep_acceptance.py` (7 `TestCase` classes) and reported it as
coverage. It is **withdrawn**: the suite asserted against a dictionary it
constructed itself, never against the emulator, and the probe it claimed to
replay (`probe_x4pro_sleep_wake.py`) could not run at all — see
[0085](0085-sleep-wake-20261031.md) for the `NameError`, the wrong QOM path and
the `"paint_rejected_by_model": not panels_unchanged or True` constant.

Both files were removed in the same commit that replaced this note. The
sleep/wake behaviour they described is now backed by a live QTest run against
the shipped kernel (`f65db0a7…`, exit `0`) recorded in
[`0085-sleep-wake-live-20261101.json`](0085-sleep-wake-live-20261101.json).

Kept as a lesson for the next batch: an acceptance test must be able to fail
when the behaviour is missing. If a test still passes after the subject under
test is deleted, it is documenting the fixture.
