"""Wellness calculators. Numbers come from code, never from an LLM (docs/AGENTS.md section 4).

Every function is pure, validates its inputs (finite, in a plausible range) and raises
``OutOfRange`` instead of returning a number for nonsense input. Formulas are published
equations; each docstring names the source so a claim can carry ``computed`` provenance.
These are population estimates for general wellness, not clinical measurements.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

CALCULATOR_VERSION = "1"


class OutOfRange(ValueError):
    """An input is non-finite or outside the range the formula was designed for."""


class Sex(StrEnum):
    # The Mifflin-St Jeor equation is published for these two values only.
    FEMALE = "female"
    MALE = "male"


class ActivityLevel(StrEnum):
    SEDENTARY = "sedentary"
    LIGHT = "light"
    MODERATE = "moderate"
    ACTIVE = "active"
    VERY_ACTIVE = "very_active"


# Standard activity factors applied to BMR (Harris-Benedict style multipliers).
ACTIVITY_FACTOR: dict[ActivityLevel, float] = {
    ActivityLevel.SEDENTARY: 1.2,
    ActivityLevel.LIGHT: 1.375,
    ActivityLevel.MODERATE: 1.55,
    ActivityLevel.ACTIVE: 1.725,
    ActivityLevel.VERY_ACTIVE: 1.9,
}


def _check(name: str, value: float, low: float, high: float) -> float:
    if not math.isfinite(value):
        raise OutOfRange(f"{name} is not a finite number")
    if not low <= value <= high:
        raise OutOfRange(f"{name}={value} is outside [{low}, {high}]")
    return value


def bmr_mifflin_st_jeor(sex: Sex, weight_kg: float, height_cm: float, age_years: float) -> float:
    """Basal metabolic rate in kcal/day (Mifflin et al., 1990). Adults only (18-100)."""
    w = _check("weight_kg", weight_kg, 25.0, 400.0)
    h = _check("height_cm", height_cm, 100.0, 250.0)
    a = _check("age_years", age_years, 18.0, 100.0)
    base = 10.0 * w + 6.25 * h - 5.0 * a
    return base + 5.0 if sex is Sex.MALE else base - 161.0


def tdee(bmr_kcal: float, level: ActivityLevel) -> float:
    """Total daily energy expenditure in kcal/day: BMR times the activity factor."""
    _check("bmr_kcal", bmr_kcal, 500.0, 6000.0)
    return bmr_kcal * ACTIVITY_FACTOR[level]


def max_heart_rate_tanaka(age_years: float) -> float:
    """Estimated maximum heart rate in bpm: 208 - 0.7 * age (Tanaka et al., 2001)."""
    return 208.0 - 0.7 * _check("age_years", age_years, 18.0, 100.0)


@dataclass(frozen=True)
class HeartRateZone:
    zone: int  # 1 (easiest) to 5 (hardest)
    low_bpm: float
    high_bpm: float


# Karvonen method: share of heart-rate reserve (HRR) per zone.
_ZONE_BOUNDS = ((0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.00))


def heart_rate_zones(age_years: float, resting_hr_bpm: float) -> list[HeartRateZone]:
    """Five Karvonen zones: rest + share * (max - rest). Zones are contiguous and ascending."""
    rest = _check("resting_hr_bpm", resting_hr_bpm, 30.0, 120.0)
    top = max_heart_rate_tanaka(age_years)
    if top <= rest:
        raise OutOfRange("resting heart rate is not below the estimated maximum")
    reserve = top - rest
    return [
        HeartRateZone(zone=i + 1, low_bpm=rest + lo * reserve, high_bpm=rest + hi * reserve)
        for i, (lo, hi) in enumerate(_ZONE_BOUNDS)
    ]


def sleep_debt_hours(slept_hours: Sequence[float], need_hours: float = 8.0) -> float:
    """Accumulated shortfall over the given nights. A long night does not erase earlier debt.

    Simple and auditable on purpose: sum of max(0, need - slept) per night. At most 30 nights.
    """
    need = _check("need_hours", need_hours, 4.0, 12.0)
    if len(slept_hours) > 30:
        raise OutOfRange("sleep debt window is limited to 30 nights")
    return sum(max(0.0, need - _check("slept_hours", h, 0.0, 24.0)) for h in slept_hours)
