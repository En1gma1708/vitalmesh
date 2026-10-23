from __future__ import annotations

from vitalmesh_agents.cohortstore import InMemoryCohortStore
from vitalmesh_agents.inprocess import ask
from vitalmesh_agents.testing import T0, world
from vitalmesh_stores import InMemoryRunLog


def run(runlog: InMemoryRunLog, **kwargs):  # type: ignore[no-untyped-def]
    bus, _, _ = world(runlog=runlog, **kwargs)
    return ask(bus, "u-1", "What should I eat and how should I train?", T0)


def test_every_event_of_a_run_lands_in_the_log_once_and_in_order() -> None:
    log = InMemoryRunLog()
    out = run(log)
    record = log.read(out.correlation_id)
    assert record is not None and record.user_id == "u-1"
    types = [e["type"] for e in record.entries]
    assert types[0] == "question.asked" and types[-1] == "recommendation.released"
    assert types.count("advice.completed") == 2  # nutrition and movement
    assert len({e["event_id"] for e in record.entries}) == len(record.entries)


def test_a_redelivered_event_adds_nothing() -> None:
    log = InMemoryRunLog()
    entry = {"event_id": "e1", "type": "question.asked", "at": "x"}
    log.append("c", "u", entry)
    log.append("c", "u", entry)
    record = log.read("c")
    assert record is not None and len(record.entries) == 1


def test_released_entry_carries_the_answer_the_claims_and_their_basis() -> None:
    log = InMemoryRunLog()
    out = run(log)
    record = log.read(out.correlation_id)
    assert record is not None
    released = record.entries[-1]
    assert released["text"] == out.text
    assert {c["basis"] for c in released["claims"]} <= {"computed", "cited", "inferred", "unknown"}
    assert released["claims"]


def test_the_cache_hit_run_is_recorded_as_tier_t1_with_no_specialist_entries() -> None:
    log = InMemoryRunLog()
    store = InMemoryCohortStore()
    bus, _, _ = world(runlog=log, cohort=store, numeric=True)
    ask(bus, "u-1", "How many calories should I eat?", T0)
    second = ask(bus, "u-2", "How many calories should I eat?", T0)
    record = log.read(second.correlation_id)
    assert record is not None
    assert next(e for e in record.entries if e["type"] == "run.planned")["tier"] == "T1"
    assert "advice.completed" not in [e["type"] for e in record.entries]
