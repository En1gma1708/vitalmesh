from __future__ import annotations

from vitalmesh_agents.budget import InMemoryLoadMonitor, classify
from vitalmesh_agents.triage import Load


def test_classify_thresholds() -> None:
    assert classify(0, 3, 6) is Load.NORMAL
    assert classify(3, 3, 6) is Load.HIGH
    assert classify(6, 3, 6) is Load.CRITICAL


def test_old_runs_age_out_of_the_window() -> None:
    now = [1000.0]
    m = InMemoryLoadMonitor(high_runs=2, critical_runs=4, window_s=60, clock=lambda: now[0])
    for _ in range(4):
        m.record()
    assert m.load() is Load.CRITICAL
    now[0] += 61
    assert m.load() is Load.NORMAL
