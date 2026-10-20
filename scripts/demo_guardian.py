"""Demo: the Safety Guardian blocks unsafe recommendations. Pure code, no stack needed.

    uv run python scripts/demo_guardian.py

Each scenario is a hand-built bad recommendation (what a buggy rule, a bad prompt or a
hallucinating agent could produce). The Guardian is independent of whatever produced it.
"""

from __future__ import annotations

from datetime import date

from vitalmesh_wellness.claims import (
    Claim,
    MealEntry,
    Provenance,
    Quantity,
    Recommendation,
    UserConstraints,
)
from vitalmesh_wellness.guardian import DEFAULT_RULES, RULES_VERSION, Guardian

SRC = "demo@1"


def claim(
    metric: str, value: float, unit: str, text: str, prov: Provenance = Provenance.COMPUTED
) -> Claim:
    return Claim(
        text=text,
        provenance=prov,
        source=None if prov is Provenance.UNKNOWN else SRC,
        quantity=Quantity(metric=metric, value=value, unit=unit),
    )


def user(**over: object) -> UserConstraints:
    base: dict[str, object] = {"user_id": "demo", "sex": "female", "age_years": 30}
    return UserConstraints(**{**base, **over})  # type: ignore[arg-type]


def rec(u: UserConstraints, *claims: Claim, plan: tuple[MealEntry, ...] = ()) -> Recommendation:
    return Recommendation(recommendation_id="demo", user=u, claims=claims, meal_plan=plan)


def crashing_rule(_rec: Recommendation) -> list:  # type: ignore[type-arg]
    raise RuntimeError("a bug inside a rule")


good_energy = claim("daily_energy_target", 2000, "kcal", "Aim for about 2000 kcal per day.")
day = date(2026, 10, 26)

SCENARIOS: list[tuple[str, Recommendation, Guardian]] = [
    ("A safe recommendation", rec(user(), good_energy), Guardian()),
    (
        "800 kcal/day for a woman (below the 1,200 floor)",
        rec(user(), claim("daily_energy_target", 800, "kcal", "Aim for about 800 kcal per day.")),
        Guardian(),
    ),
    (
        "Energy sent in kilojoules (a unit mix-up is blocked, not converted)",
        rec(user(), claim("daily_energy_target", 8368, "kJ", "Aim for 8368 kJ per day.")),
        Guardian(),
    ),
    (
        "A claim nobody can vouch for (unknown provenance is never safe)",
        rec(user(), claim("sleep_target", 8, "h", "Sleep 8 hours.", Provenance.UNKNOWN)),
        Guardian(),
    ),
    (
        "Hard training zone 5 for a user with a heart condition",
        rec(
            user(conditions=frozenset({"cardiac"})),
            claim("target_hr_zone", 5, "zone", "Train in zone 5."),
        ),
        Guardian(),
    ),
    (
        "Peanut-butter snack for a nut-allergic vegan",
        rec(
            user(diet="vegan", allergens=frozenset({"nuts"})),
            good_energy,
            plan=(MealEntry(day=day, meal="snack", recipe_id="s01", scale=1.0),),
        ),
        Guardian(),
    ),
    (
        "Wheat dish for a celiac user who listed no allergen (the graph implies gluten)",
        rec(
            user(conditions=frozenset({"celiac_disease"})),
            good_energy,
            plan=(MealEntry(day=day, meal="lunch", recipe_id="l03", scale=1.0),),
        ),
        Guardian(),
    ),
    (
        "A recipe id that does not exist (an agent invented a dish)",
        rec(
            user(),
            good_energy,
            plan=(MealEntry(day=day, meal="lunch", recipe_id="zzz", scale=1.0),),
        ),
        Guardian(),
    ),
    (
        "A rule that crashes (the check cannot run, so it BLOCKS: fail closed)",
        rec(user(), good_energy),
        Guardian(rules=(*DEFAULT_RULES, crashing_rule)),
    ),
]


def main() -> None:
    print(f"Safety Guardian, rules version {RULES_VERSION}\n")
    for title, recommendation, guardian in SCENARIOS:
        verdict = guardian.evaluate(recommendation)
        mark = "CLEARED" if verdict.cleared else "BLOCKED"
        print(f"[{mark}] {title}")
        for v in verdict.violations:
            print(f"          - {v.rule}: {v.message}")
    print("\nNothing blocked above ever reaches the released topic or the user.")


if __name__ == "__main__":
    main()
