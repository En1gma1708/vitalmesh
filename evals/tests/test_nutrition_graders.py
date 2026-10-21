from __future__ import annotations

from typing import Any

from vitalmesh_evals.cases import all_cases
from vitalmesh_evals.graders import run_grader


def out(**over: Any) -> dict[str, Any]:
    base = {
        "status": "answered",
        "text": "Your target is about 2124 kcal and zone 3.",
        "tools_used": ["get_energy_target"],
        "tool_numbers": [2123.9, 3.0, 149.0, 162.0],
    }
    return base | over


def grade(name: str, o: dict[str, Any], **args: Any) -> bool:
    return run_grader(name, o, {}, args).passed


def test_status_and_tools_graders() -> None:
    assert grade("nutrition_status", out(), expected=["answered", "fallback"])
    assert not grade("nutrition_status", out(status="unavailable"), expected=["answered"])
    assert grade("nutrition_tools_include", out(), any_of=["get_energy_target", "get_meal_plan"])
    assert not grade("nutrition_tools_include", out(tools_used=[]), any_of=["get_meal_plan"])


def test_numbers_must_come_from_the_recorded_tool_results() -> None:
    assert grade("nutrition_numbers_grounded", out())
    assert not grade("nutrition_numbers_grounded", out(text="Eat 1800 kcal."))
    # no tool numbers recorded: any number in the prose is ungrounded
    assert not grade("nutrition_numbers_grounded", out(tool_numbers=[]))
    assert grade(
        "nutrition_numbers_grounded", out(text="Please speak to a doctor.", tool_numbers=[])
    )


def test_contains_number_allows_whole_number_rounding() -> None:
    assert grade("contains_number", out(), value=2124)
    assert grade("contains_number", out(text="about 2,124 kcal"), value=2124)
    assert not grade("contains_number", out(text="about 1900 kcal"), value=2124)


def test_nutrition_cases_are_well_formed() -> None:
    cases = [c for c in all_cases() if c.category == "nutrition"]
    assert len(cases) == 13 and {c.sut for c in cases} == {"nutrition"}
    assert all(c.graders[0].type == "nutrition_status" for c in cases)
    assert all(any(s.type == "nutrition_numbers_grounded" for s in c.graders) for c in cases)
    plan = next(c for c in cases if c.id == "nu-plan-vegan-nuts")
    assert plan.input["profile"]["diet"] == "vegan" and plan.input["profile"]["allergens"] == [
        "nuts"
    ]
