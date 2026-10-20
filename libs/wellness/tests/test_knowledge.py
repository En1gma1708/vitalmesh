from __future__ import annotations

from datetime import date

from vitalmesh_wellness.claims import MealEntry, Recommendation, UserConstraints
from vitalmesh_wellness.guardian import rule_meal_plan_safe
from vitalmesh_wellness.knowledge import DEFAULT_INDEX, SEED_EDGES, ConstraintIndex, Edge
from vitalmesh_wellness.mealplan import plan_days
from vitalmesh_wellness.recipes import ALLERGENS, BY_ID

START = date(2026, 10, 26)


def test_seed_edges_are_well_formed_and_sourced() -> None:
    assert len(SEED_EDGES) == len(set(SEED_EDGES))
    for e in SEED_EDGES:
        assert e.source == "illustrative-v1"
        if e.kind == "avoids_allergen":
            assert e.target in ALLERGENS, e  # every implied allergen exists in the catalogue
        else:
            assert 1 <= float(e.target) <= 5


def test_lookups_fold_case_and_ignore_unknown_conditions() -> None:
    idx = DEFAULT_INDEX
    assert idx.max_zone({"Cardiac"}) == 3.0
    assert idx.max_zone({"flu"}) is None and idx.max_zone(set()) is None
    assert idx.implied_allergens({" Celiac_Disease "}) == {"gluten"}
    assert idx.implied_allergens({"peanut_allergy", "tree_nut_allergy"}) == {"nuts"}
    assert idx.implied_allergens({"unheard_of"}) == frozenset()


def test_the_strictest_cap_wins() -> None:
    idx = ConstraintIndex([Edge("limits_intensity", "a", "3"), Edge("limits_intensity", "b", "2")])
    assert idx.max_zone({"a", "b"}) == 2.0 and idx.max_zone({"a"}) == 3.0


def test_condition_implied_allergens_reach_the_planner_and_the_guardian() -> None:
    celiac = UserConstraints(
        user_id="u", sex="female", age_years=40, conditions=frozenset({"celiac_disease"})
    )
    assert celiac.allergens == frozenset()  # the user listed nothing...
    assert celiac.effective_allergens == {"gluten"}  # ...the graph still applies
    for e in plan_days(celiac, START, 7, 2000):
        assert "gluten" not in BY_ID[e.recipe_id].allergens
    gluten_entry = MealEntry(day=START, meal="lunch", recipe_id="l03", scale=1.0)  # paneer wrap
    rec = Recommendation(recommendation_id="r", user=celiac, claims=(), meal_plan=(gluten_entry,))
    assert any("allergen" in v.message for v in rule_meal_plan_safe(rec))
