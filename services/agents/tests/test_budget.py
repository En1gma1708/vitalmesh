from __future__ import annotations

from vitalmesh_agents.budget import (
    TIER_TOKEN_COST,
    InMemoryLoadMonitor,
    InMemoryTierCounter,
    classify,
)
from vitalmesh_agents.triage import Load


def test_classify_thresholds_are_fractions_of_the_budget() -> None:
    assert classify(0, 1000) is Load.NORMAL
    assert classify(599, 1000) is Load.NORMAL
    assert classify(600, 1000) is Load.HIGH
    assert classify(1000, 1000) is Load.CRITICAL


def test_one_full_graph_run_exceeds_the_free_tier_minute() -> None:
    """The arithmetic the free-tier defaults rest on (8,000 tokens a minute, 12,200 per T3 run)."""
    m = InMemoryLoadMonitor()
    assert m.load() is Load.NORMAL
    m.record("T3")
    assert TIER_TOKEN_COST["T3"] > 8000 and m.load() is Load.CRITICAL


def test_cache_hits_and_shed_runs_cost_no_tokens() -> None:
    m = InMemoryLoadMonitor(tokens_per_window=1000)
    for tier in ("T0", "T1") * 50:
        m.record(tier)
    assert m.load() is Load.NORMAL


def test_old_runs_age_out_of_the_window() -> None:
    now = [1000.0]
    m = InMemoryLoadMonitor(tokens_per_window=20_000, window_s=60, clock=lambda: now[0])
    m.record("T3")
    assert m.load() is Load.HIGH
    now[0] += 61
    assert m.load() is Load.NORMAL


def test_tier_counter_counts_per_tier() -> None:
    c = InMemoryTierCounter()
    for t in ("T1", "T1", "T3"):
        c.add(t)
    assert c.counts() == {"T1": 2, "T3": 1}
