"""Seed two synthetic users of the SAME cohort, with no conditions and no labs, for the T1 demo.

    uv run python scripts/seed_cohort.py [prefix]      # creates <prefix>-a and <prefix>-b

Same sex, age band, activity, goal, diet and allergens (so the same cohort key), different bodies
(so different calculated numbers). demo-1 is not usable for this: its demo labs include an
out-of-range LDL, and a user with a flagged lab never shares a cached answer (D70).
"""

from __future__ import annotations

import os
import sys

import redis
from vitalmesh_t0.profiles import ProfileRecord, RedisProfileStore
from vitalmesh_wellness.synthetic import synthetic_user


def main() -> None:
    prefix = sys.argv[1] if len(sys.argv) > 1 else "cohort"
    client = redis.Redis.from_url(os.environ.get("VITALMESH_REDIS_URL", "redis://127.0.0.1:6379/0"))
    store = RedisProfileStore(client)
    base, sleep = synthetic_user(2026, 7)
    base = base.model_copy(update={"conditions": frozenset(), "medications": frozenset()})
    bodies = {"a": (72.0, 178.0, 35.0), "b": (86.0, 183.0, 38.0)}  # weight kg, height cm, age
    for suffix, (weight, height, age) in bodies.items():
        profile = base.model_copy(
            update={
                "user_id": f"{prefix}-{suffix}",
                "weight_kg": weight,
                "height_cm": height,
                "age_years": age,
            }
        )
        store.put(ProfileRecord(profile=profile, recent_sleep_hours=tuple(sleep)))
        print(
            f"seeded {profile.user_id}: {profile.sex.value}, {age:.0f}y, {weight} kg, "
            f"{profile.activity.value}, diet {profile.diet}, allergens {sorted(profile.allergens)}"
        )


if __name__ == "__main__":
    main()
