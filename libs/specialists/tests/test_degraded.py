"""A helper whose model is unavailable still answers from its pure-code tools (D100).

Before this, ``llm.unavailable`` made the helper report "couldn't finish" and the run ended partial
or refused. Now ``advise_or_degrade`` runs the helper's own deterministic tools instead; the text is
built only from their results (same fallback a failed draft gets), so nothing unchecked is added.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from vitalmesh_llm import AllProvidersFailed, LLMRequest, LLMResponse
from vitalmesh_specialists import ConditionSpecialist, MovementSpecialist, NutritionSpecialist
from vitalmesh_specialists.agent import SpecialistUnavailable, ToolsLike, ToolSpecialist
from vitalmesh_specialists.condition import ConditionTools
from vitalmesh_specialists.movement import MovementTools
from vitalmesh_specialists.tools import NutritionTools
from vitalmesh_stores import InMemoryHistoryStore, InMemoryLabStore, Point
from vitalmesh_wellness.engine import Profile

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


def profile(**extra: Any) -> Profile:
    return Profile.model_validate(
        {
            "user_id": "t", "sex": "female", "age_years": 30, "weight_kg": 65,
            "height_cm": 165, "activity": "moderate", "resting_hr_bpm": 60, "goal": "maintain",
            **extra,
        }
    )  # fmt: skip


class Insufficient:
    """An Evidence agent that must never be reached: the no-model path does not call it."""

    def answer(self, question: str) -> Any:
        raise AssertionError("the guideline search needs a model, so the no-model path skips it")


class DownRouter:
    """Every provider fails, every time."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, req: LLMRequest, max_wait_s: float = 0.0) -> LLMResponse:
        self.calls += 1
        raise AllProvidersFailed(["groq: HTTP 429", "gemini: HTTP 503"])


def no_sleep(_: float) -> None:
    return None


def movement_tools() -> MovementTools:
    return MovementTools(profile(), [7.0] * 14, InMemoryHistoryStore(), Insufficient(), NOW)


def nutrition_tools() -> NutritionTools:
    return NutritionTools(profile(diet="vegan"), [7.0] * 14, Insufficient(), NOW.date())


def condition_tools() -> ConditionTools:
    return ConditionTools(
        profile(conditions=["cardiac"]), InMemoryLabStore().list("t"), Insufficient()
    )


CASES = [
    ("movement", MovementSpecialist, movement_tools,
     {"get_training_targets", "get_activity_summary"}),
    ("nutrition", NutritionSpecialist, nutrition_tools, {"get_energy_target", "get_meal_plan"}),
    ("condition", ConditionSpecialist, condition_tools, {"get_constraints", "get_lab_summary"}),
]  # fmt: skip


@pytest.mark.parametrize(("name", "cls", "make_tools", "expected"), CASES)
def test_a_down_model_still_gives_the_helpers_own_checked_facts(
    name: str, cls: Callable[..., ToolSpecialist], make_tools: Any, expected: set[str]
) -> None:
    router = DownRouter()
    helper = cls(router, attempts=2, retry_pause_s=0.0, sleep=no_sleep)
    advice = helper.advise_or_degrade(f"what about my {name}?", make_tools())
    assert advice.status == "fallback"
    assert advice.model_calls == 0
    assert set(advice.tools_used) == expected
    assert advice.reason.startswith("no model was available")
    assert advice.text and "could not find checked information" not in advice.text
    assert all(r.ok for r in advice.results)


def test_the_plain_advise_still_raises_so_callers_can_tell_the_difference() -> None:
    helper = MovementSpecialist(DownRouter(), attempts=1, sleep=no_sleep)
    with pytest.raises(SpecialistUnavailable):
        helper.advise("how should I train?", movement_tools())


def test_the_movement_text_has_the_users_zone_and_nothing_the_tools_did_not_say() -> None:
    helper = MovementSpecialist(DownRouter(), attempts=1, sleep=no_sleep)
    advice = helper.advise_or_degrade("should i do cardio or gym?", movement_tools())
    ordered = sorted(advice.results, key=lambda r: r.name != "get_workout_plan")  # D105: it leads
    claims = [
        c["text"]
        for r in ordered
        for c in r.payload.get("claims", [])
        if c["metric"] != "target_hr_zone"  # a concrete workout replaces the general zone line
    ]
    assert claims, "the tools returned no claims"
    assert advice.text == " ".join(claims)  # exactly the tool claims, nothing added
    assert any(
        c["metric"] == "target_hr_zone" for r in advice.results for c in r.payload.get("claims", [])
    )


def test_the_activity_goal_appears_when_there_is_history() -> None:
    history = InMemoryHistoryStore()
    for d in range(1, 20):
        history.add("t", "active_minutes", Point(t=NOW - timedelta(days=d), value=30.0, unit="min"))
    tools = MovementTools(profile(), [7.0] * 14, history, Insufficient(), NOW)
    advice = MovementSpecialist(DownRouter(), attempts=1, sleep=no_sleep).advise_or_degrade(
        "cardio or gym?", tools
    )
    assert "weekly goal is 150" in advice.text


def test_an_exhausted_time_budget_stops_asking_the_model_and_degrades() -> None:
    router = DownRouter()
    helper = MovementSpecialist(router, attempts=1, sleep=no_sleep, time_budget_s=0.0)
    advice = helper.advise_or_degrade("how should I train?", movement_tools())
    assert advice.status == "fallback"
    assert router.calls == 0  # the budget was already used up: the model was never asked
    assert "time budget" in advice.reason


def test_a_tool_that_raises_does_not_take_the_helper_down() -> None:
    class Broken:
        specs = ()

        def default_calls(self, question: str) -> tuple[tuple[str, str], ...]:
            return (("get_training_targets", "{}"), ("get_activity_summary", "{}"))

        def call(self, name: str, arguments: str) -> Any:
            if name == "get_training_targets":
                raise RuntimeError("boom")
            return movement_tools().call(name, arguments)

    tools: ToolsLike = Broken()
    advice = MovementSpecialist(DownRouter(), attempts=1, sleep=no_sleep).advise_or_degrade(
        "how should I train?", tools
    )
    assert advice.status == "fallback"
    assert advice.tools_used == ("get_activity_summary",)
    assert any(not r.ok and r.error == "the tool failed" for r in advice.results)
