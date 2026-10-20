"""Seed one synthetic demo user straight into Redis (profile, 90 days of history, a few labs).

    uv run python scripts/seed_demo.py [user_id]

Everything here is synthetic. It writes through the same store classes the services use, so the
API and the chart page at /app/<user_id> read it back exactly as they would real data.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

import redis
from vitalmesh_processor.history import Point, RedisHistoryStore
from vitalmesh_processor.labstore import RedisLabStore
from vitalmesh_t0.profiles import ProfileRecord, RedisProfileStore
from vitalmesh_wellness.engine import build_recommendation
from vitalmesh_wellness.labs import interpret
from vitalmesh_wellness.synthetic import synthetic_history, synthetic_user

# Made-up results in the shape labs print them (name, value, unit, printed range).
DEMO_LABS = (
    ("Glucose Fasting", 92, "mg/dL", "70 - 100"),
    ("HbA1c", 5.4, "%", "< 5.7"),
    ("Cholesterol Total", 196, "mg/dL", "< 200"),
    ("HDL Cholesterol", 52, "mg/dL", "> 40"),
    ("LDL Cholesterol", 128, "mg/dL", "< 100"),
    ("Triglycerides", 118, "mg/dL", "< 150"),
    ("Creatinine", 0.9, "mg/dL", "0.6 - 1.3"),
    ("TSH", 2.1, "mIU/L", "0.4 - 4.0"),
)


def main() -> None:
    user_id = sys.argv[1] if len(sys.argv) > 1 else "demo-1"
    client = redis.Redis.from_url(os.environ.get("VITALMESH_REDIS_URL", "redis://127.0.0.1:6379/0"))
    now = datetime.now(UTC).replace(hour=8, minute=0, second=0, microsecond=0)

    profile, sleep = synthetic_user(2026, 7)
    profile = profile.model_copy(update={"user_id": user_id, "conditions": frozenset()})
    RedisProfileStore(client).put(ProfileRecord(profile=profile, recent_sleep_hours=tuple(sleep)))

    target = next(
        c.quantity.value
        for c in build_recommendation(profile, sleep, "seed").claims
        if c.quantity and c.quantity.metric == "daily_energy_target"
    )
    history = RedisHistoryStore(client)
    for metric, points in synthetic_history(2026, profile, 90, now, intake_center=target).items():
        unit = {
            "body_weight": "kg",
            "sleep_duration": "h",
            "active_minutes": "min",
            "step_count": "count",
            "energy_intake": "kcal",
        }[metric]
        for t, v in points:
            history.add(user_id, metric, Point(t, v, unit))

    labs = RedisLabStore(client)
    for name, value, unit, ref in DEMO_LABS:
        labs.put(
            user_id,
            interpret(name, now - timedelta(days=14), value=value, unit=unit, reference_text=ref),
        )
    print(
        f"seeded {user_id}: {profile.sex.value}, {profile.age_years:.0f}y, goal "
        f"{profile.goal.value}, diet {profile.diet}, allergens {sorted(profile.allergens)}"
    )
    print(f"open http://127.0.0.1:8000/app/{user_id} after: uv run python -m vitalmesh_api")


if __name__ == "__main__":
    main()
