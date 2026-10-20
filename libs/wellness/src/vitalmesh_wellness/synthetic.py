"""Seeded synthetic users. Same seed and index always give the same user (no PII, no real data)."""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from vitalmesh_wellness.calculators import ActivityLevel, Sex
from vitalmesh_wellness.engine import Goal, Profile

_CONDITIONS = ("cardiac", "uncontrolled_hypertension")
_DIETS = ("omnivore", "omnivore", "vegetarian", "vegan")
_ALLERGENS = ("nuts", "dairy", "gluten", "egg", "fish", "shellfish", "soy", "sesame")


def synthetic_user(seed: int, index: int) -> tuple[Profile, list[float]]:
    """Return a profile and 14 nights of sleep. Independent streams per (seed, index)."""
    rng = random.Random(f"{seed}:{index}")
    sex = rng.choice(list(Sex))
    conditions = frozenset(c for c in _CONDITIONS if rng.random() < 0.08)
    profile = Profile(
        user_id=f"u{seed}-{index:07d}",
        sex=sex,
        age_years=float(rng.randint(18, 80)),
        weight_kg=round(rng.uniform(45, 130), 1),
        height_cm=round(rng.uniform(150, 195), 1),
        activity=rng.choice(list(ActivityLevel)),
        resting_hr_bpm=float(rng.randint(48, 90)),
        goal=rng.choice(list(Goal)),
        conditions=conditions,
    )
    sleep = [round(min(max(rng.gauss(7.0, 1.3), 3.0), 10.5), 1) for _ in range(14)]
    # drawn last so the fields above keep their values for existing seeds
    profile = profile.model_copy(
        update={
            "diet": rng.choice(_DIETS),
            "allergens": frozenset(a for a in _ALLERGENS if rng.random() < 0.08),
        }
    )
    return profile, sleep


def synthetic_history(
    seed: int, profile: Profile, days: int, end: datetime, intake_center: float = 2000.0
) -> dict[str, list[tuple[datetime, float]]]:
    """Daily synthetic readings for the last ``days`` days up to ``end`` (deterministic).

    Weight follows a slow trend that matches the goal plus noise; sleep, steps, active minutes
    and intake vary day to day. All values stay inside the plausible ranges the API accepts.
    """
    rng = random.Random(f"{seed}:{profile.user_id}:history")
    trend = {"lose": -0.04, "gain": 0.03, "maintain": 0.0}[profile.goal.value]
    activity_minutes = {
        "sedentary": 15,
        "light": 25,
        "moderate": 40,
        "active": 55,
        "very_active": 70,
    }.get(profile.activity.value, 30)
    out: dict[str, list[tuple[datetime, float]]] = {
        k: []
        for k in ("body_weight", "sleep_duration", "active_minutes", "step_count", "energy_intake")
    }
    weight = profile.weight_kg
    for i in range(days):
        day = end - timedelta(days=days - 1 - i)
        weight = min(max(weight + trend + rng.gauss(0, 0.25), 25.0), 300.0)
        active = max(0.0, rng.gauss(activity_minutes, activity_minutes * 0.4))
        out["body_weight"].append((day, round(weight, 1)))
        out["sleep_duration"].append((day, round(min(max(rng.gauss(7.1, 1.1), 3.0), 10.5), 1)))
        out["active_minutes"].append((day, round(min(active, 300.0))))
        out["step_count"].append((day, round(min(active * 110 + rng.gauss(2500, 900), 40000))))
        out["energy_intake"].append(
            (day, round(min(max(rng.gauss(intake_center, 350), 900.0), 4500.0)))
        )
    return out
