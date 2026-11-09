"""Exact-boundary and immutability tests for the Safety Guardian (Phase 7, D93).

These exist because in-memory mutation testing (`scripts/mutation_guardian.py`) showed that the
plan-energy rule's boundaries (the floor, the ceiling, the 15% tolerance, the claim epsilon) and
the immutability of verdicts were not pinned by any test: a one-character change to them would
have passed the whole suite. Each test uses a one-recipe-a-day catalogue with chosen energy values
so the total lands exactly ON the boundary, where `<` versus `<=` matters.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, timedelta

import pytest
from pydantic import ValidationError
from vitalmesh_wellness import guardian
from vitalmesh_wellness.claims import MealEntry, Recommendation
from vitalmesh_wellness.engine import build_meal_plan_recommendation
from vitalmesh_wellness.guardian import (
    METRIC_ENERGY,
    METRIC_PLAN_ENERGY,
    Guardian,
    Verdict,
    Violation,
    rule_meal_plan_energy,
)
from vitalmesh_wellness.recipes import BY_ID, Diet, Meal, Recipe
from vitalmesh_wellness.synthetic import synthetic_user

START = date(2026, 10, 26)


def plan(
    monkeypatch: pytest.MonkeyPatch,
    day_kcal: list[float],
    *,
    target: float,
    claimed: float,
    sex: str = "female",
) -> Recommendation:
    """A plan with exactly one planned meal per day whose energy is ``day_kcal[i]``."""
    profile, sleep = synthetic_user(1, 3)
    profile = profile.model_copy(update={"conditions": frozenset()})
    rec = build_meal_plan_recommendation(profile, sleep, "r-boundary", START)
    entries = []
    for i, kcal in enumerate(day_kcal):
        rid = f"boundary-{i}"
        monkeypatch.setitem(
            BY_ID,
            rid,
            Recipe(
                id=rid, title="x", meal=Meal.LUNCH, kcal=kcal, protein_g=0, carbs_g=0, fat_g=0,
                diet=Diet.VEGAN,
            ),
        )  # fmt: skip
        entries.append(
            MealEntry(day=START + timedelta(days=i), meal="lunch", recipe_id=rid, scale=1.0)
        )

    def set_value(metric: str, value: float) -> tuple[object, ...]:
        return tuple(
            c.model_copy(update={"quantity": c.quantity.model_copy(update={"value": value})})
            if c.quantity and c.quantity.metric == metric
            else c
            for c in rec.claims
        )

    claims = set_value(METRIC_ENERGY, target)
    rec2 = rec.model_copy(update={"claims": claims, "meal_plan": tuple(entries)})
    claims = tuple(
        c.model_copy(update={"quantity": c.quantity.model_copy(update={"value": claimed})})
        if c.quantity and c.quantity.metric == METRIC_PLAN_ENERGY
        else c
        for c in rec2.claims
    )
    user = rec2.user.model_copy(update={"sex": sex})
    return rec2.model_copy(update={"claims": claims, "user": user})


def messages(rec: Recommendation) -> list[str]:
    return [v.message for v in rule_meal_plan_energy(rec)]


# ------------------------------------------------------------------------ the claim epsilon
def test_claim_within_half_a_kcal_of_the_plan_is_accepted_exactly_at_the_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert messages(plan(monkeypatch, [2000.0, 2000.0], target=2000.0, claimed=2000.5)) == []
    assert messages(plan(monkeypatch, [2000.0, 2000.0], target=2000.0, claimed=1999.5)) == []


def test_claim_a_whole_kcal_off_the_plan_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    for claimed in (2001.0, 1999.0):
        msgs = messages(plan(monkeypatch, [2000.0, 2000.0], target=2000.0, claimed=claimed))
        assert any("differs" in m for m in msgs), claimed
    assert guardian.PLAN_TOTAL_EPSILON_KCAL == 0.5  # the documented tolerance, not a free knob


# ------------------------------------------------------------ the energy floor and ceiling
def test_a_day_exactly_at_the_female_floor_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    assert messages(plan(monkeypatch, [1200.0], target=1200.0, claimed=1200.0)) == []


def test_a_day_just_under_the_floor_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    msgs = messages(plan(monkeypatch, [1199.0], target=1200.0, claimed=1199.0))
    assert any("outside" in m for m in msgs)


def test_a_day_exactly_at_the_ceiling_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    assert messages(plan(monkeypatch, [6000.0], target=6000.0, claimed=6000.0)) == []


def test_a_day_just_over_the_ceiling_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    msgs = messages(plan(monkeypatch, [6001.0], target=6001.0, claimed=6001.0))
    assert any("outside" in m for m in msgs)


def test_the_male_floor_is_higher(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = plan(monkeypatch, [1500.0], target=1500.0, claimed=1500.0, sex="male")
    assert messages(ok) == []
    low = plan(monkeypatch, [1400.0], target=1500.0, claimed=1400.0, sex="male")
    assert any("outside" in m for m in messages(low))


# --------------------------------------------------------------------------- the 15% window
def test_a_day_exactly_fifteen_percent_from_the_target_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert messages(plan(monkeypatch, [2300.0], target=2000.0, claimed=2300.0)) == []
    assert messages(plan(monkeypatch, [1700.0], target=2000.0, claimed=1700.0)) == []


def test_a_day_just_past_fifteen_percent_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    for kcal in (2301.0, 1699.0):
        msgs = messages(plan(monkeypatch, [kcal], target=2000.0, claimed=kcal))
        assert any("away from the target" in m for m in msgs), kcal


# ------------------------------------------------------- violations come out in day order
def test_violations_are_reported_in_day_order_not_in_size_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 900 on the first day, 1100 on the second: by value or by text ("1100.0" < "900.0") the order
    # would flip, so only a day-keyed sort passes
    rec = plan(monkeypatch, [900.0, 1100.0], target=1200.0, claimed=1000.0)
    outside = [m for m in messages(rec) if "outside" in m]
    assert len(outside) == 2
    assert outside[0].startswith(str(START)) and outside[1].startswith(str(START + timedelta(1)))


# ------------------------------------------------------------------------------ immutability
def test_verdicts_violations_and_the_guardian_cannot_be_changed_after_the_fact() -> None:
    v = Violation(rule="r", message="m")
    with pytest.raises(ValidationError):
        v.message = "changed"  # type: ignore[misc]
    verdict = Verdict(cleared=True, violations=())
    with pytest.raises(ValidationError):
        verdict.cleared = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        Guardian().rules = ()  # type: ignore[misc]
