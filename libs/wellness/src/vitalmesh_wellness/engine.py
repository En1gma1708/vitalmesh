"""T0 rule engine: user profile and recent signals in, a ``Recommendation`` out. No LLM.

Every number is produced by ``calculators`` and carried as a ``computed`` claim. The engine
never calls the Guardian: the Guardian is an independent check on whatever the engine (or
any agent) produced, so a bug here is caught there. Rules below are deliberately simple and
auditable; a user with an intensity-limiting condition is capped at zone 3 here as well,
and the Guardian enforces the same cap independently.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from vitalmesh_wellness import calculators as calc
from vitalmesh_wellness.claims import Claim, Provenance, Quantity, Recommendation, UserConstraints
from vitalmesh_wellness.guardian import (
    ENERGY_FLOOR_KCAL,
    METRIC_ENERGY,
    METRIC_HR_ZONE,
    METRIC_PLAN_ENERGY,
    METRIC_SLEEP,
    METRIC_WORKOUT_EXERCISE_SETS,
    METRIC_WORKOUT_MINUTES,
    METRIC_WORKOUT_TOTAL_SETS,
    METRIC_WORKOUT_ZONE,
)
from vitalmesh_wellness.knowledge import DEFAULT_INDEX
from vitalmesh_wellness.mealplan import plan_days
from vitalmesh_wellness.recipes import BY_ID
from vitalmesh_wellness.workouts import (
    BY_ID as EXERCISES,
)
from vitalmesh_wellness.workouts import (
    PLANNER_VERSION as WORKOUT_PLANNER_VERSION,
)
from vitalmesh_wellness.workouts import (
    SESSION_LABEL,
    WARMUP_MINUTES,
    Place,
    plan_workout,
)

ENGINE_VERSION = "1"
_SOURCE = f"vitalmesh_wellness.calculators@{calc.CALCULATOR_VERSION}/engine@{ENGINE_VERSION}"

DEFAULT_ZONE = 3
RECOVERY_ZONE = 2
SLEEP_DEBT_RECOVERY_HOURS = 7.0
BASE_SLEEP_TARGET_H = 8.0
EXTRA_SLEEP_TARGET_H = 9.0
GOAL_ENERGY_FACTOR = {"maintain": 1.0, "lose": 0.85, "gain": 1.10}


class Goal(StrEnum):
    MAINTAIN = "maintain"
    LOSE = "lose"
    GAIN = "gain"


class Profile(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str = Field(min_length=1)
    sex: calc.Sex
    age_years: float
    weight_kg: float
    height_cm: float
    activity: calc.ActivityLevel
    resting_hr_bpm: float
    goal: Goal = Goal.MAINTAIN
    conditions: frozenset[str] = frozenset()
    medications: frozenset[str] = frozenset()
    diet: str = "omnivore"
    allergens: frozenset[str] = frozenset()

    def constraints(self) -> UserConstraints:
        return UserConstraints(
            user_id=self.user_id,
            sex=self.sex.value,
            age_years=self.age_years,
            conditions=self.conditions,
            medications=self.medications,
            diet=self.diet,
            allergens=self.allergens,
        )


def _computed(metric: str, value: float, unit: str, text: str) -> Claim:
    return Claim(
        text=text,
        provenance=Provenance.COMPUTED,
        source=_SOURCE,
        quantity=Quantity(metric=metric, value=value, unit=unit),
    )


def build_recommendation(
    profile: Profile, recent_sleep_hours: Sequence[float], recommendation_id: str
) -> Recommendation:
    """Raises ``calculators.OutOfRange`` for implausible input; never guesses a number."""
    bmr = calc.bmr_mifflin_st_jeor(
        profile.sex, profile.weight_kg, profile.height_cm, profile.age_years
    )
    maintenance = calc.tdee(bmr, profile.activity)
    floor = ENERGY_FLOOR_KCAL[profile.sex.value]
    energy = max(maintenance * GOAL_ENERGY_FACTOR[profile.goal.value], floor)
    floored = energy == floor and maintenance * GOAL_ENERGY_FACTOR[profile.goal.value] < floor

    debt = calc.sleep_debt_hours(recent_sleep_hours)
    recovering = debt >= SLEEP_DEBT_RECOVERY_HOURS
    zone = RECOVERY_ZONE if recovering else DEFAULT_ZONE
    cap = DEFAULT_INDEX.max_zone(profile.conditions)
    if cap is not None:
        zone = min(zone, int(cap))
    zones = calc.heart_rate_zones(profile.age_years, profile.resting_hr_bpm)
    target = zones[zone - 1]
    sleep_target = EXTRA_SLEEP_TARGET_H if recovering else BASE_SLEEP_TARGET_H

    energy_text = f"Aim for about {energy:.0f} kcal per day ({profile.goal.value})."
    if floored:
        energy_text += " The target was raised to the minimum safe floor."
    claims = (
        _computed(METRIC_ENERGY, round(energy, 1), "kcal", energy_text),
        _computed(
            METRIC_HR_ZONE,
            float(zone),
            "zone",
            f"Train in zone {zone} ({target.low_bpm:.0f}-{target.high_bpm:.0f} bpm)"
            + (f"; recent sleep debt is {debt:.1f} h, so keep it easy." if recovering else "."),
        ),
        _computed(METRIC_SLEEP, sleep_target, "h", f"Aim for {sleep_target:g} hours of sleep."),
    )
    return Recommendation(
        recommendation_id=recommendation_id, user=profile.constraints(), claims=claims
    )


def build_meal_plan_recommendation(
    profile: Profile,
    recent_sleep_hours: Sequence[float],
    recommendation_id: str,
    start: date,
    days: int = 7,
) -> Recommendation:
    """The usual recommendation plus a meal plan. Raises ``PlanError`` when no safe plan exists.

    The plan's average energy is a ``computed`` claim; the Guardian recomputes every day's total
    from the catalogue and rejects any mismatch.
    """
    base = build_recommendation(profile, recent_sleep_hours, recommendation_id)
    target = next(
        c.quantity.value for c in base.claims if c.quantity and c.quantity.metric == METRIC_ENERGY
    )
    entries = plan_days(base.user, start, days, target)
    totals: dict[date, float] = {}
    for e in entries:
        totals[e.day] = totals.get(e.day, 0.0) + BY_ID[e.recipe_id].kcal * e.scale
    mean = sum(totals.values()) / len(totals)
    claim = _computed(
        METRIC_PLAN_ENERGY,
        round(mean, 1),
        "kcal",
        f"Your {days}-day plan averages about {mean:.0f} kcal per day.",
    )
    return base.model_copy(update={"claims": (*base.claims, claim), "meal_plan": entries})


WORKOUT_SOURCE = f"vitalmesh_wellness.workouts@{WORKOUT_PLANNER_VERSION}"
_WHY_TEXT = {
    "recovery": " Your recent sleep debt calls for an easy day.",
    "conditions": (
        " Because of the health conditions you listed, keep it easy and check with your "
        "doctor before starting a training plan."
    ),
    "short": " With this little time, easy cardio is the best fit.",
}


def build_workout_recommendation(
    profile: Profile,
    recent_sleep_hours: Sequence[float],
    recommendation_id: str,
    day: date,
    place: Place,
    minutes: int,
    focus: str | None = None,
) -> Recommendation:
    """The usual recommendation plus today's workout: easy cardio in the user's zone, or a
    beginner strength session of named exercises. Raises ``WorkoutError`` for impossible input.

    Every number is a ``computed`` claim; the Guardian re-checks the bounds, the conditions and
    (when the plan is attached) every exercise against the catalogue.
    """
    base = build_recommendation(profile, recent_sleep_hours, recommendation_id)
    recovering = calc.sleep_debt_hours(recent_sleep_hours) >= SLEEP_DEBT_RECOVERY_HOURS
    plan = plan_workout(base.user, day, place, minutes, recovering, focus)
    where = "at the gym" if place is Place.GYM else "at home"
    claims: list[Claim] = []
    if plan.cardio_zone is not None:
        zone = calc.heart_rate_zones(profile.age_years, profile.resting_hr_bpm)[
            plan.cardio_zone - 1
        ]
        text = (
            f"Today: {plan.minutes} minutes of easy cardio (a brisk walk, cycling or a light "
            f"jog), at a pace where you can still talk." + _WHY_TEXT.get(plan.why, "")
        )
        claims.append(_computed(METRIC_WORKOUT_MINUTES, float(plan.minutes), "min", text))
        claims.append(
            _computed(
                METRIC_WORKOUT_ZONE,
                float(plan.cardio_zone),
                "zone",
                f"Easy pace is zone {plan.cardio_zone}: "
                f"{zone.low_bpm:.0f}-{zone.high_bpm:.0f} bpm.",
            )
        )
    else:
        label = SESSION_LABEL[plan.session]
        text = (
            f"Today: {label} {where}, about {plan.minutes} minutes including a "
            f"{WARMUP_MINUTES}-minute easy warm-up."
        )
        claims.append(_computed(METRIC_WORKOUT_MINUTES, float(plan.minutes), "min", text))
        for e in plan.entries:
            ex = EXERCISES[e.exercise_id]
            claims.append(
                _computed(
                    METRIC_WORKOUT_EXERCISE_SETS,
                    float(e.sets),
                    "sets",
                    f"{ex.name}: {e.sets} sets of {e.reps_low}-{e.reps_high} {ex.unit}.",
                )
            )
        total = sum(e.sets for e in plan.entries)
        claims.append(
            _computed(
                METRIC_WORKOUT_TOTAL_SETS,
                float(total),
                "sets",
                f"That is {total} sets in total. Rest about 60 to 90 seconds between sets and "
                "pick a weight you can control.",
            )
        )
    return base.model_copy(update={"claims": (*base.claims, *claims), "workout_plan": plan.entries})
