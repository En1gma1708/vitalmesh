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


def test_tier_counter_counts_per_tier() -> None:
    from vitalmesh_agents.budget import InMemoryTierCounter

    c = InMemoryTierCounter()
    for t in ("T1", "T1", "T3"):
        c.add(t)
    assert c.counts() == {"T1": 2, "T3": 1}
