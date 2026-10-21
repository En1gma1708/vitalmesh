from __future__ import annotations

from vitalmesh_agents.cohortstore import InMemoryCohortStore
from vitalmesh_agents.inprocess import ask
from vitalmesh_agents.testing import T0, world

QUESTION = "How many calories should I eat?"


def test_a_second_user_of_the_same_cohort_is_answered_without_a_model() -> None:
    store = InMemoryCohortStore()
    bus, router, _ = world(cohort=store, numeric=True)

    first = ask(bus, "u-1", QUESTION, T0)
    assert first.final == "released" and first.errors == ()
    assert store.stats()["fill"] == 1 and store.stats()["miss"] == 1
    calls_after_first = len(router.purposes)
    assert calls_after_first > 0

    second = ask(bus, "u-2", QUESTION, T0)
    assert second.final == "released" and second.errors == ()
    assert len(router.purposes) == calls_after_first  # not one more model call
    planned = next(s for s in second.events if s.meta.type == "run.planned")
    assert planned.meta.tier is not None and planned.meta.tier.value == "T1"
    assert "cache.cohort_hit" in list(planned.payload.rule_ids)
    assert store.stats()["hit"] == 1
    # personalised: u-2's own number, not u-1's
    assert first.text != second.text
    assert "kcal per day" in second.text


def test_the_t1_answer_still_goes_through_the_guardian_gate() -> None:
    store = InMemoryCohortStore()
    bus, _, _ = world(cohort=store, numeric=True)
    ask(bus, "u-1", QUESTION, T0)
    second = ask(bus, "u-2", QUESTION, T0)
    types = [s.meta.type for s in second.events]
    assert "safety.cleared" in types and types[-1] == "recommendation.released"
    assert "advice.completed" not in types  # no specialist ran


def test_a_user_with_a_condition_never_uses_or_fills_the_cache() -> None:
    store = InMemoryCohortStore()
    bus, _, _ = world(cohort=store, numeric=True, conditions=["celiac_disease"])
    ask(bus, "u-1", QUESTION, T0)
    ask(bus, "u-2", QUESTION, T0)
    assert store.stats()["fill"] == 0 and store.stats()["hit"] == 0


def test_a_vanished_entry_falls_back_to_a_full_run() -> None:
    store = InMemoryCohortStore()
    bus, router, _ = world(cohort=store, numeric=True)
    ask(bus, "u-1", QUESTION, T0)
    store._entries.clear()  # evicted between Triage's check and the cohort handler
    store.put = lambda *_a, **_k: None  # type: ignore[method-assign]  # and not refilled
    real_get = store.get
    calls = {"n": 0}

    def flaky(key: str):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return object() if calls["n"] == 1 else real_get(key)  # Triage sees a hit, handler a miss

    store.get = flaky  # type: ignore[method-assign]
    out = ask(bus, "u-2", QUESTION, T0)
    assert out.final == "released" and out.errors == ()
    assert store.stats()["personalise_failed"] == 1
    assert "advice.completed" in [s.meta.type for s in out.events]  # the full graph ran
    assert len(router.purposes) > 0
