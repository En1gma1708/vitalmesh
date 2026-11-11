"""Everyday questions through the whole graph (D108): routed to the progress helper, answered
from its calculators, released by the Guardian, with every model down and under critical load."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest
from test_outage_end_to_end import advice_statuses, fail_when
from vitalmesh_agents.inprocess import ask
from vitalmesh_agents.specialist_handler import scoped_question
from vitalmesh_agents.testing import T0, world
from vitalmesh_agents.triage import Load
from vitalmesh_common.run import Specialist
from vitalmesh_llm import AllProvidersFailed, LLMRequest, LLMResponse
from vitalmesh_specialists import MovementSpecialist, MovementTools
from vitalmesh_stores import InMemoryHistoryStore
from vitalmesh_wellness.engine import Profile


def released_claims(out: Any) -> list[Any]:
    released = next(s for s in out.events if s.meta.type == "recommendation.released")
    return list(released.payload.claims)


def planned_specialists(out: Any) -> list[int]:
    planned = next(s for s in out.events if s.meta.type == "run.planned")
    return list(planned.payload.specialists)


@pytest.mark.parametrize(
    "question",
    [
        "how am i doing",
        "how was my sleep",
        "how is my weight",
        "what should my target weight be",
        "what is my bmi",
    ],
)
def test_an_everyday_question_is_released_with_every_model_down(
    monkeypatch: pytest.MonkeyPatch, question: str
) -> None:
    fail_when(monkeypatch, lambda purpose: True)
    bus, _router, _clock = world()
    out = ask(bus, "u-1", question, T0)
    assert out.final == "released", (question, out.final, out.errors)
    assert out.errors == () and out.text.strip()
    assert len(planned_specialists(out)) == 1  # the progress helper alone: no food or workout
    assert set(advice_statuses(out).values()) == {"fallback"}


def test_the_target_weight_answer_is_a_range_for_the_height_with_the_caveat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fail_when(monkeypatch, lambda purpose: True)
    bus, _router, _clock = world()  # u-1 is 65 kg, 165 cm: BMI 23.9
    out = ask(bus, "u-1", "what should my target weight be", T0)
    assert out.final == "released"
    text = out.text
    assert "50.4 to 67.7 kg" in text and "BMI is 23.9" in text
    assert "ideal" not in text.lower()
    # every number the user is shown was produced by a claim (the gate's own rule, re-checked)
    claim_text = " ".join(c.text for c in released_claims(out))
    for number in re.findall(r"\d+(?:\.\d+)?", text):
        assert number in claim_text, number


def test_sleep_with_no_logged_nights_says_it_has_no_data_instead_of_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fail_when(monkeypatch, lambda purpose: True)
    bus, _router, _clock = world()
    out = ask(bus, "u-1", "how was my sleep", T0)
    assert out.final == "released"
    assert "need at least 3" in out.text or "Log your sleep" in out.text


def test_a_mixed_question_is_answered_by_two_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    fail_when(monkeypatch, lambda purpose: True)
    bus, _router, _clock = world()
    out = ask(bus, "u-1", "how is my weight and what should i eat", T0)
    assert out.final == "released", (out.final, out.errors)
    assert len(planned_specialists(out)) == 2


def test_with_the_model_up_the_scripted_helper_is_used_and_the_answer_still_releases() -> None:
    bus, router, _clock = world()
    out = ask(bus, "u-1", "how am i doing", T0)
    assert out.final == "released", (out.final, out.errors)
    assert "progress-specialist" in router.purposes


def test_under_critical_load_a_progress_question_is_answered_with_no_model_at_all() -> None:
    bus, router, _clock = world(load=Load.CRITICAL)
    out = ask(bus, "u-1", "what is my bmi", T0)
    assert out.final == "released", (out.final, out.errors)
    assert router.purposes == []  # no model call anywhere
    assert "BMI is 23.9" in out.text
    drafted = next(s for s in out.events if s.meta.type == "recommendation.drafted")
    assert drafted.payload.used_fallback is True
    assert len(drafted.payload.citations) >= 1  # the BMI bands, quoted from the NIH report
    assert drafted.payload.citations[0].document == "nhlbi-obesity-1998"


class _Down:
    def complete(self, req: LLMRequest, max_wait_s: float = 0.0) -> LLMResponse:
        raise AllProvidersFailed(["groq: HTTP 429"])


PROFILE = Profile.model_validate(
    {
        "user_id": "t", "sex": "female", "age_years": 30, "weight_kg": 65,
        "height_cm": 165, "activity": "moderate", "resting_hr_bpm": 60, "goal": "maintain",
    }
)  # fmt: skip


def test_the_movement_helper_is_not_given_a_workout_for_a_question_that_is_not_about_one() -> None:
    class NoEvidence:
        def answer(self, question: str) -> Any:
            raise AssertionError("not needed")

    scoped = scoped_question(Specialist.MOVEMENT_RECOVERY, "how is my recovery going")
    mt = MovementTools(
        PROFILE,
        [7.0] * 14,
        InMemoryHistoryStore(),
        NoEvidence(),
        datetime(2026, 11, 11, 9, 0, tzinfo=UTC),
    )
    advice = MovementSpecialist(_Down(), attempts=1, sleep=lambda _s: None).advise_or_degrade(
        scoped, mt
    )
    assert "get_workout_plan" not in advice.tools_used
