from __future__ import annotations

from datetime import timedelta

from vitalmesh_agents.inprocess import ask, outcome_of
from vitalmesh_agents.testing import T0, world
from vitalmesh_common.run import Specialist


def test_a_low_risk_question_is_released_through_the_real_handlers() -> None:
    bus, router, _ = world()
    out = ask(bus, "u-1", "What should I eat and how should I train?", T0)
    assert out.final == "released" and out.errors == ()
    assert out.text.startswith("Here is a short plan")
    types = [s.meta.type for s in out.events]
    assert types[0] == "question.asked" and types[1] == "run.planned"
    assert {
        "advice.completed",
        "advice.bundle.ready",
        "recommendation.drafted",
        "safety.cleared",
    } <= set(types)
    assert "checker" not in router.purposes  # low risk: no independent review
    assert router.purposes.count("synthesizer") == 1


def test_a_condition_makes_it_medium_risk_so_the_checker_runs() -> None:
    bus, router, _ = world(conditions=["celiac_disease"])
    out = ask(bus, "u-1", "What should I eat?", T0)
    assert out.final == "released"
    assert router.purposes.count("checker") == 1


def test_a_marked_lab_goes_to_a_human_and_nothing_is_released() -> None:
    bus, router, _ = world(labs=True)
    out = ask(bus, "u-1", "What should I eat?", T0)
    assert out.final == "review_requested"
    assert not out.events or all(s.meta.type != "recommendation.released" for s in out.events)
    assert "briefer" in router.purposes


def test_one_revise_verdict_buys_exactly_one_rewrite_then_a_human() -> None:
    bus, router, _ = world(conditions=["celiac_disease"], verdicts=["revise", "revise"])
    out = ask(bus, "u-1", "What should I eat?", T0)
    assert out.final == "review_requested"
    assert router.purposes.count("synthesizer") == 2  # first draft + the single rewrite
    assert router.purposes.count("checker") == 2


def test_revise_then_approve_is_released_after_the_rewrite() -> None:
    bus, router, _ = world(conditions=["celiac_disease"], verdicts=["revise", "approve"])
    out = ask(bus, "u-1", "What should I eat?", T0)
    assert out.final == "released" and router.purposes.count("synthesizer") == 2


def test_a_missing_specialist_is_released_partial_after_the_deadline_sweep() -> None:
    bus, _, clock = world(specialists=frozenset({Specialist.NUTRITION}))
    first = ask(bus, "u-1", "What should I eat and how should I train?", T0)
    assert first.final == "none"  # waiting for the movement specialist
    clock.now = T0 + timedelta(seconds=200)
    assert bus.sweep() == 1
    bus.drain()
    out = outcome_of(bus, first.correlation_id)
    assert out.final == "released" and "partial" in out.text.lower()


def test_an_exception_in_a_handler_is_recorded_not_swallowed() -> None:
    bus, _, _ = world()
    out = ask(bus, "nobody", "What should I eat?", T0)  # no profile: Triage raises
    assert out.final == "none" and any("UnknownUser" in e for e in out.errors)
