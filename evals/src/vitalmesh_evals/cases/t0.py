"""Golden cases for the deterministic path (engine, meal planner, Guardian).

Expected numbers are literals. Two were also worked by hand from the Mifflin-St Jeor equation
(female, 30 y, 65 kg, 165 cm: 10*65 + 6.25*165 - 5*30 - 161 = 1370.25, times 1.55 = 2123.9; male,
30 y, 80 kg, 180 cm: 1780 times 1.55 = 2759.0), so the goldens do not depend on the engine.
"""

from __future__ import annotations

from typing import Any

from vitalmesh_evals.case import EvalCase, g

BASE: dict[str, Any] = {
    "user_id": "g",
    "sex": "female",
    "age_years": 30,
    "weight_kg": 65,
    "height_cm": 165,
    "activity": "moderate",
    "resting_hr_bpm": 60,
    "goal": "maintain",
}
GOOD_SLEEP = [8.0] * 14
DEBT_SLEEP = [5.5] * 14
PLAN = {"start": "2026-10-26", "days": 7}


def profile(**over: Any) -> dict[str, Any]:
    return {**BASE, **over}


def case(id_: str, category: str, inp: dict[str, Any], *graders: Any, note: str = "") -> EvalCase:
    return EvalCase(id=id_, category=category, sut="t0", input=inp, graders=graders, note=note)


def numbers(
    id_: str,
    over: dict[str, Any],
    energy: float,
    zone: float,
    sleep_h: float,
    sleep: list[float] | None = None,
    note: str = "",
) -> EvalCase:
    return case(
        id_,
        "t0_numbers",
        {"profile": profile(**over), "sleep": sleep or GOOD_SLEEP},
        g("guardian_cleared"),
        g("claim_equals", metric="daily_energy_target", value=energy, tol=0.1),
        g("claim_equals", metric="target_hr_zone", value=zone),
        g("claim_equals", metric="sleep_target", value=sleep_h),
        g("claim_unit", metric="daily_energy_target", unit="kcal"),
        note=note,
    )


NUMBERS = (
    numbers("t0-female-maintain", {}, 2123.9, 3, 8, note="hand-worked: 1370.25 x 1.55"),
    numbers("t0-female-lose", {"goal": "lose"}, 1805.3, 3, 8, note="0.85 x maintenance"),
    numbers("t0-female-gain", {"goal": "gain"}, 2336.3, 3, 8, note="1.10 x maintenance"),
    numbers(
        "t0-male-maintain",
        {"sex": "male", "weight_kg": 80, "height_cm": 180},
        2759.0,
        3,
        8,
        note="hand-worked: 1780 x 1.55",
    ),
    numbers("t0-young-adult", {"age_years": 18}, 2216.9, 3, 8),
    numbers("t0-older-adult", {"age_years": 80}, 1736.4, 3, 8),
    numbers(
        "t0-high-resting-hr",
        {"resting_hr_bpm": 90},
        2123.9,
        3,
        8,
        note="resting HR changes zone bounds, not the energy target",
    ),
    numbers(
        "t0-sleep-debt-recovery",
        {},
        2123.9,
        2,
        9,
        sleep=DEBT_SLEEP,
        note="14 nights at 5.5 h: easy zone and a longer sleep target",
    ),
    numbers(
        "t0-female-floor",
        {
            "weight_kg": 40,
            "height_cm": 150,
            "activity": "sedentary",
            "goal": "lose",
            "age_years": 60,
        },
        1200.0,
        3,
        8,
        note="target raised to the minimum safe floor",
    ),
    numbers(
        "t0-male-floor",
        {
            "sex": "male",
            "weight_kg": 45,
            "height_cm": 150,
            "activity": "sedentary",
            "goal": "lose",
            "age_years": 80,
        },
        1500.0,
        3,
        8,
    ),
)

SAFETY = (
    case(
        "t0-floor-is-disclosed",
        "t0_safety",
        {
            "profile": profile(
                weight_kg=40, height_cm=150, activity="sedentary", goal="lose", age_years=60
            )
        },
        g("text_contains", metric="daily_energy_target", substring="minimum safe floor"),
    ),
    case(
        "t0-cardiac-capped",
        "t0_safety",
        {"profile": profile(conditions=["cardiac"])},
        g("guardian_cleared"),
        g("claim_equals", metric="target_hr_zone", value=3),
    ),
    case(
        "t0-hypertension-capped-and-in-debt",
        "t0_safety",
        {"profile": profile(conditions=["uncontrolled_hypertension"]), "sleep": DEBT_SLEEP},
        g("guardian_cleared"),
        g("claim_equals", metric="target_hr_zone", value=2),
    ),
    case(
        "t0-condition-case-and-spacing-folded",
        "t0_safety",
        {"profile": profile(conditions=[" CARDIAC "])},
        g("guardian_cleared"),
        g("claim_equals", metric="target_hr_zone", value=3),
    ),
    case(
        "t0-implausible-weight-rejected",
        "t0_safety",
        {"profile": profile(weight_kg=5)},
        g("error_is", expected="out_of_range"),
        note="a number is never guessed from implausible input",
    ),
    case(
        "t0-implausible-age-rejected",
        "t0_safety",
        {"profile": profile(age_years=-3)},
        g("error_is", expected="out_of_range"),
    ),
    case(
        "t0-unknown-activity-rejected",
        "t0_safety",
        {"profile": profile(activity="couch-athlete")},
        g("error_is", expected="invalid_profile"),
    ),
    case(
        "t0-extreme-target-never-unsafe",
        "t0_safety",
        {
            "profile": profile(
                sex="male",
                weight_kg=125,
                height_cm=195,
                activity="very_active",
                goal="gain",
                age_years=25,
            ),
            "plan": PLAN,
        },
        g("plan_safe_or_refused"),
        note="a 4,900 kcal target cannot be reached within portion limits: withheld, not served",
    ),
)

PLANS = (
    case(
        "plan-vegan-no-nuts",
        "t0_plan",
        {"profile": profile(diet="vegan", allergens=["nuts"]), "plan": PLAN},
        g("guardian_cleared"),
        g("plan_days", days=7),
        g("plan_has_no_allergen", allergens=["nuts"]),
        g("plan_fits_diet", diet="vegan"),
    ),
    case(
        "plan-celiac-implied-gluten",
        "t0_plan",
        {"profile": profile(conditions=["celiac_disease"]), "plan": PLAN},
        g("guardian_cleared"),
        g("plan_has_no_allergen", allergens=[]),
        note="the user listed no allergens; the knowledge graph implies gluten",
    ),
    case(
        "plan-vegetarian",
        "t0_plan",
        {"profile": profile(diet="vegetarian"), "plan": PLAN},
        g("guardian_cleared"),
        g("plan_fits_diet", diet="vegetarian"),
    ),
    case(
        "plan-many-allergens-vegan",
        "t0_plan",
        {
            "profile": profile(
                diet="vegan", allergens=["nuts", "dairy", "gluten", "egg", "soy", "sesame"]
            ),
            "plan": PLAN,
        },
        g("guardian_cleared"),
        g("plan_has_no_allergen", allergens=["nuts", "dairy", "gluten", "egg", "soy", "sesame"]),
    ),
    case(
        "plan-fish-and-shellfish-allergy",
        "t0_plan",
        {"profile": profile(allergens=["fish", "shellfish"]), "plan": PLAN},
        g("guardian_cleared"),
        g("plan_has_no_allergen", allergens=["fish", "shellfish"]),
    ),
    case(
        "plan-two-days",
        "t0_plan",
        {"profile": profile(), "plan": {"start": "2026-10-26", "days": 2}},
        g("guardian_cleared"),
        g("plan_days", days=2),
    ),
    case(
        "plan-unknown-diet-refused",
        "t0_plan",
        {"profile": profile(diet="pescatarian"), "plan": PLAN},
        g("plan_refused"),
        note="an unknown diet is not guessed",
    ),
    case(
        "plan-unverifiable-allergen-blocked",
        "t0_plan",
        {"profile": profile(allergens=["kiwi"]), "plan": PLAN},
        g("guardian_cleared", expected=False),
        note="an allergen the catalogue cannot check blocks the plan (fails closed)",
    ),
)

CASES: tuple[EvalCase, ...] = NUMBERS + SAFETY + PLANS
