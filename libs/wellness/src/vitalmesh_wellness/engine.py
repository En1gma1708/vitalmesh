"""T0 rule engine: user profile and recent signals in, a ``Recommendation`` out. No LLM.

Every number is produced by ``calculators`` and carried as a ``computed`` claim. The engine
never calls the Guardian: the Guardian is an independent check on whatever the engine (or
any agent) produced, so a bug here is caught there. Rules below are deliberately simple and
auditable; a user with an intensity-limiting condition is capped at zone 3 here as well,
and the Guardian enforces the same cap independently.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from vitalmesh_wellness import calculators as calc
from vitalmesh_wellness.claims import Claim, Provenance, Quantity, Recommendation, UserConstraints
from vitalmesh_wellness.guardian import (
    ENERGY_FLOOR_KCAL,
    INTENSITY_LIMITING_CONDITIONS,
    METRIC_ENERGY,
    METRIC_HR_ZONE,
    METRIC_SLEEP,
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

    def constraints(self) -> UserConstraints:
        return UserConstraints(
            user_id=self.user_id,
            sex=self.sex.value,
            age_years=self.age_years,
            conditions=self.conditions,
            medications=self.medications,
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
    if profile.conditions & INTENSITY_LIMITING_CONDITIONS:
        zone = min(zone, 3)
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
