"""Rule-driven, deterministic meal planner (D52). No LLM.

Same inputs always give the same plan. A day's energy target is split across the meals; for
each slot the planner takes the least-recently-used eligible recipe (ties broken by a stable
hash, so different users and days vary without randomness) and scales the portion to the slot
budget in quarter steps within [MIN_SCALE, MAX_SCALE].

The planner is not trusted: the Guardian's ``meal_plan`` rules re-check every entry against
the catalogue (allergens, diet, portion range, totals, target) independently of this module.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Sequence
from datetime import date, timedelta

from vitalmesh_wellness.claims import MealEntry, UserConstraints
from vitalmesh_wellness.recipes import (
    CATALOGUE,
    DIET_NAMES,
    PORTION_MAX,
    PORTION_MIN,
    Meal,
    Recipe,
)

PLANNER_VERSION = "1"
MIN_SCALE = PORTION_MIN
MAX_SCALE = PORTION_MAX
MEAL_SHARE = {Meal.BREAKFAST: 0.25, Meal.LUNCH: 0.35, Meal.DINNER: 0.30, Meal.SNACK: 0.10}
MEAL_ORDER = (Meal.BREAKFAST, Meal.LUNCH, Meal.DINNER, Meal.SNACK)
TARGET_TOLERANCE = 0.15  # a day must land within 15% of the energy target


class PlanError(ValueError):
    """No safe plan can be made for this user (for example every option is excluded)."""


def eligible(recipe: Recipe, user: UserConstraints) -> bool:
    diet = DIET_NAMES.get(user.diet)
    if diet is None:  # an unknown diet is not guessed
        return False
    return recipe.diet <= diet and not (recipe.allergens & user.allergens)


def _tiebreak(user_id: str, day: date, meal: Meal, recipe_id: str) -> int:
    digest = hashlib.sha256(f"{user_id}|{day.isoformat()}|{meal.value}|{recipe_id}".encode())
    return int.from_bytes(digest.digest()[:8], "big")


def _rank(
    used: Counter[tuple[Meal, str]], user_id: str, day: date, meal: Meal, recipe: Recipe
) -> tuple[int, int]:
    return (used[(meal, recipe.id)], _tiebreak(user_id, day, meal, recipe.id))


def _scale_for(budget: float, recipe: Recipe) -> float:
    raw = budget / recipe.kcal
    stepped = round(raw * 4) / 4
    return min(max(stepped, MIN_SCALE), MAX_SCALE)


def plan_days(
    user: UserConstraints,
    start: date,
    days: int,
    daily_kcal: float,
    catalogue: Sequence[Recipe] = CATALOGUE,
) -> tuple[MealEntry, ...]:
    if not 1 <= days <= 14:
        raise PlanError("a plan covers 1 to 14 days")
    if daily_kcal <= 0:
        raise PlanError("a plan needs a positive energy target")
    options = {m: [r for r in catalogue if r.meal is m and eligible(r, user)] for m in MEAL_ORDER}
    for meal, found in options.items():
        if not found:
            raise PlanError(f"no {meal.value} option fits this user's diet and allergens")

    used: Counter[tuple[Meal, str]] = Counter()
    entries: list[MealEntry] = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        for meal in MEAL_ORDER:
            pick = min(
                options[meal],
                key=lambda r: _rank(used, user.user_id, day, meal, r),
            )
            used[(meal, pick.id)] += 1
            entries.append(
                MealEntry(
                    day=day,
                    meal=meal.value,
                    recipe_id=pick.id,
                    scale=_scale_for(daily_kcal * MEAL_SHARE[meal], pick),
                )
            )
    return tuple(entries)
