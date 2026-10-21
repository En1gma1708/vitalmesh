from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vitalmesh_agents.inprocess import Bus, Wiring, ask, build_graph, outcome_of
from vitalmesh_agents.specialist_handler import SpecialistDeps
from vitalmesh_agents.testing import NoEvidence, ScriptedRouter
from vitalmesh_common.run import Specialist
from vitalmesh_stores import (
    InMemoryHistoryStore,
    InMemoryLabStore,
    InMemoryProfileStore,
    ProfileRecord,
)
from vitalmesh_wellness.engine import Profile
from vitalmesh_wellness.labs import interpret

T0 = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def world(
    conditions: list[str] | None = None,
    verdicts: list[str] | None = None,
    specialists: frozenset[Specialist] = frozenset(Specialist),
    labs: bool = False,
) -> tuple[Bus, ScriptedRouter, Clock]:
    profiles = InMemoryProfileStore()
    profiles.put(
        ProfileRecord(
            profile=Profile.model_validate(
                {
                    "user_id": "u-1",
                    "sex": "female",
                    "age_years": 30,
                    "weight_kg": 65,
                    "height_cm": 165,
                    "activity": "moderate",
                    "resting_hr_bpm": 60,
                    "conditions": conditions or [],
                }
            ),
            recent_sleep_hours=tuple([8.0] * 14),
        )
    )
    lab_store = InMemoryLabStore()
    if labs:
        lab_store.put(
            "u-1",
            interpret(
                "LDL Cholesterol",
                T0 - timedelta(days=3),
                value=190,
                unit="mg/dL",
                reference_text="< 100",
            ),
        )
    router, clock = ScriptedRouter(verdicts), Clock()
    deps = SpecialistDeps(
        router=router,
        evidence=NoEvidence(),
        profiles=profiles,
        labs=lab_store,
        history=InMemoryHistoryStore(),
        sleep=lambda _s: None,
    )
    bus = build_graph(
        Wiring(router=router, deps=deps, profiles=profiles, clock=clock, specialists=specialists)
    )
    return bus, router, clock


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
