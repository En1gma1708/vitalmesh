"""Seeded synthetic users. Same seed and index always give the same user (no PII, no real data)."""

from __future__ import annotations

import random

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
