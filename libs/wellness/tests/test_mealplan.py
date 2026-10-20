from __future__ import annotations

from collections import Counter
from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from vitalmesh_wellness.claims import MealEntry, Recommendation, UserConstraints
from vitalmesh_wellness.engine import build_meal_plan_recommendation
from vitalmesh_wellness.guardian import (
    METRIC_PLAN_ENERGY,
    Guardian,
    rule_meal_plan_energy,
    rule_meal_plan_safe,
)
from vitalmesh_wellness.mealplan import MEAL_ORDER, PlanError, eligible, plan_days
from vitalmesh_wellness.recipes import ALLERGENS, BY_ID, CATALOGUE, Diet, Meal
from vitalmesh_wellness.synthetic import synthetic_user

START = date(2026, 10, 26)


def user(diet: str = "omnivore", allergens: frozenset[str] = frozenset()) -> UserConstraints:
    return UserConstraints(
        user_id="u-1", sex="female", age_years=30, diet=diet, allergens=allergens
    )


# ---------------------------------------------------------------------------- catalogue
def test_catalogue_is_consistent() -> None:
    ids = [r.id for r in CATALOGUE]
    assert len(ids) == len(set(ids))
    for r in CATALOGUE:
        assert r.allergens <= ALLERGENS, r.id
        macro_kcal = 4 * r.protein_g + 4 * r.carbs_g + 9 * r.fat_g
        assert macro_kcal == pytest.approx(r.kcal, rel=0.10), r.id  # macros explain the energy


@pytest.mark.parametrize("allergen", sorted(ALLERGENS))
def test_every_meal_has_a_vegan_option_for_each_single_allergen(allergen: str) -> None:
    u = user("vegan", frozenset({allergen}))
    for meal in MEAL_ORDER:
        assert any(r.meal is meal and eligible(r, u) for r in CATALOGUE), (allergen, meal)


# ------------------------------------------------------------------------------ planner
def test_plan_is_deterministic_and_users_differ() -> None:
    a = plan_days(user(), START, 7, 2000)
    assert a == plan_days(user(), START, 7, 2000)
    other = UserConstraints(user_id="u-2", sex="female", age_years=30)
    assert a != plan_days(other, START, 7, 2000)
    assert len(a) == 7 * 4
    assert [e.meal for e in a[:4]] == [m.value for m in MEAL_ORDER]


def test_least_recently_used_gives_even_variety() -> None:
    plan = plan_days(user(), START, 7, 2000)
    for meal in MEAL_ORDER:
        counts = Counter(e.recipe_id for e in plan if e.meal == meal.value)
        n_options = sum(1 for r in CATALOGUE if r.meal is meal)
        assert max(counts.values()) <= -(-7 // n_options), (meal, counts)


def test_diet_and_allergen_constraints_are_respected() -> None:
    u = user("vegan", frozenset({"nuts", "gluten", "soy"}))
    for e in plan_days(u, START, 14, 2200):
        r = BY_ID[e.recipe_id]
        assert r.diet is Diet.VEGAN and not (r.allergens & u.allergens)


@pytest.mark.parametrize("days", [0, 15, -1])
def test_day_count_is_bounded(days: int) -> None:
    with pytest.raises(PlanError):
        plan_days(user(), START, days, 2000)


def test_non_positive_target_and_impossible_user_are_errors() -> None:
    with pytest.raises(PlanError):
        plan_days(user(), START, 7, 0)
    with pytest.raises(PlanError):
        only_allergenic = [r for r in CATALOGUE if r.allergens]  # nothing safe remains
        plan_days(user("vegan", ALLERGENS), START, 7, 2000, catalogue=only_allergenic)
    with pytest.raises(PlanError):
        plan_days(user("pescatarian"), START, 7, 2000)  # an unknown diet is not guessed


@settings(max_examples=60, deadline=None)
@given(
    st.sampled_from(["omnivore", "vegetarian", "vegan"]),
    st.frozensets(st.sampled_from(sorted(ALLERGENS)), max_size=4),
    st.floats(min_value=1200, max_value=3500),
)
def test_planner_output_never_violates_the_independent_safety_rule(
    diet: str, allergens: frozenset[str], kcal: float
) -> None:
    u = user(diet, allergens)
    try:
        plan = plan_days(u, START, 7, kcal)
    except PlanError:
        return
    rec = Recommendation(recommendation_id="r", user=u, claims=(), meal_plan=plan)
    assert rule_meal_plan_safe(rec) == []


# ------------------------------------------------------------- engine plus Guardian
def test_synthetic_users_get_plans_that_the_guardian_judges_independently() -> None:
    cleared = unplannable = blocked = 0
    for i in range(300):
        profile, sleep = synthetic_user(21, i)
        try:
            rec = build_meal_plan_recommendation(profile, sleep, f"r{i}", START)
        except PlanError:
            unplannable += 1
            continue
        verdict = Guardian().evaluate(rec)
        if verdict.cleared:
            cleared += 1
            for e in rec.meal_plan:  # a cleared plan really is safe for this person
                assert not (BY_ID[e.recipe_id].allergens & profile.allergens)
        else:
            blocked += 1
            # the only acceptable reason is energy (a target the portion limits cannot reach)
            assert {v.rule for v in verdict.violations} <= {"meal_plan_energy", "energy_bounds"}
    assert cleared + blocked + unplannable == 300
    assert cleared > 200  # most users get a plan; the rest are refused, never served unsafely


# ------------------------------------------------------------------ Guardian negatives
def good(allergens: frozenset[str] = frozenset(), diet: str = "omnivore") -> Recommendation:
    profile, sleep = synthetic_user(1, 3)
    profile = profile.model_copy(
        update={"diet": diet, "allergens": allergens, "conditions": frozenset()}
    )
    return build_meal_plan_recommendation(profile, sleep, "r-good", START)


def with_plan(rec: Recommendation, entries: tuple[MealEntry, ...]) -> Recommendation:
    return rec.model_copy(update={"meal_plan": entries})


def test_a_good_plan_is_cleared() -> None:
    assert Guardian().evaluate(good()).cleared


def test_allergen_in_plan_is_blocked_even_if_planner_claims_otherwise() -> None:
    rec = good()
    swapped = tuple(
        e.model_copy(update={"recipe_id": "b05"}) if e.meal == "breakfast" else e
        for e in rec.meal_plan
    )
    nut_user = rec.user.model_copy(update={"allergens": frozenset({"nuts"})})
    bad = with_plan(rec, swapped).model_copy(update={"user": nut_user})
    assert any("allergen" in v.message for v in rule_meal_plan_safe(bad))
    assert not Guardian().evaluate(bad).cleared


@pytest.mark.parametrize(
    ("change", "expect"),
    [
        ({"recipe_id": "zzz"}, "unknown recipe"),
        ({"meal": "dinner"}, "wrong meal slot"),
        ({"scale": 3.0}, "portion out of range"),
        ({"scale": 0.1}, "portion out of range"),
    ],
)
def test_malformed_entries_are_blocked(change: dict[str, object], expect: str) -> None:
    rec = good()
    first = rec.meal_plan[0].model_copy(update=change)
    bad = with_plan(rec, (first, *rec.meal_plan[1:]))
    assert any(expect in v.message for v in rule_meal_plan_safe(bad))
    assert not Guardian().evaluate(bad).cleared


def test_vegan_user_cannot_be_given_an_egg_dish() -> None:
    rec = good(diet="vegan")
    egg = rec.meal_plan[0].model_copy(update={"recipe_id": "b03"})
    bad = with_plan(rec, (egg, *rec.meal_plan[1:]))
    assert any("diet" in v.message for v in rule_meal_plan_safe(bad))


def test_unverifiable_allergen_or_diet_blocks() -> None:
    rec = good()
    odd_allergen = rec.model_copy(
        update={"user": rec.user.model_copy(update={"allergens": frozenset({"kiwi"})})}
    )
    assert any("cannot verify" in v.message for v in rule_meal_plan_safe(odd_allergen))
    odd_diet = rec.model_copy(update={"user": rec.user.model_copy(update={"diet": "pescatarian"})})
    assert any("unknown diet" in v.message for v in rule_meal_plan_safe(odd_diet))


def test_energy_claim_must_match_the_plan_and_the_target() -> None:
    rec = good()
    lied = tuple(
        c.model_copy(update={"quantity": c.quantity.model_copy(update={"value": 9999.0})})
        if c.quantity and c.quantity.metric == METRIC_PLAN_ENERGY
        else c
        for c in rec.claims
    )
    lied_rec = rec.model_copy(update={"claims": lied})
    assert any("differs" in v.message for v in rule_meal_plan_energy(lied_rec))
    halved = tuple(e.model_copy(update={"scale": 0.5}) for e in rec.meal_plan)
    msgs = [v.message for v in rule_meal_plan_energy(with_plan(rec, halved))]
    assert any("away from the target" in m for m in msgs)


def test_plan_without_its_claims_is_blocked() -> None:
    rec = good()
    kept = tuple(
        c for c in rec.claims if not (c.quantity and c.quantity.metric == METRIC_PLAN_ENERGY)
    )
    assert rule_meal_plan_energy(rec.model_copy(update={"claims": kept}))


def test_every_day_has_every_meal_once() -> None:
    per_day = Counter(e.day for e in good().meal_plan)
    assert set(per_day.values()) == {len(Meal)}
