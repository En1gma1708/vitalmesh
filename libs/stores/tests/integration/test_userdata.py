from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import redis
from vitalmesh_stores import (
    Point,
    ProfileRecord,
    RedisHistoryStore,
    RedisLabStore,
    RedisProfileStore,
    export_user,
    purge_user,
)
from vitalmesh_wellness.engine import Profile
from vitalmesh_wellness.labs import interpret

pytestmark = pytest.mark.integration


def seed(client: redis.Redis, uid: str) -> None:
    profile = Profile.model_validate(
        {
            "user_id": uid,
            "sex": "female",
            "age_years": 30,
            "weight_kg": 65,
            "height_cm": 165,
            "activity": "moderate",
            "resting_hr_bpm": 60,
        }
    )
    RedisProfileStore(client).put(ProfileRecord(profile=profile))
    now = datetime.now(UTC)
    RedisHistoryStore(client).add(uid, "body_weight", Point(now, 65.0, "kg"))
    RedisLabStore(client).put(
        uid, interpret("LDL Cholesterol", now, value=128, unit="mg/dL", reference_text="< 100")
    )


def test_export_returns_what_is_held_and_purge_removes_only_that_person(
    redis_client: redis.Redis,
) -> None:
    a, b = f"t-{uuid.uuid4().hex[:8]}", f"t-{uuid.uuid4().hex[:8]}"
    seed(redis_client, a)
    seed(redis_client, b)
    held = export_user(redis_client, a)
    assert len(held["labs"]) == 1 and list(held["history"]) == ["body_weight"]
    assert purge_user(redis_client, a) == 3  # profile, labs, one history series
    assert export_user(redis_client, a) == {"labs": [], "history": {}}
    assert RedisProfileStore(redis_client).get(a) is None
    assert RedisProfileStore(redis_client).get(b) is not None  # the other person is untouched
    purge_user(redis_client, b)


@pytest.mark.parametrize("bad", ["", "a*", "a:b", "x" * 65, "../etc", "u 1"])
def test_unsafe_ids_never_reach_a_key_or_a_scan_pattern(
    redis_client: redis.Redis, bad: str
) -> None:
    with pytest.raises(ValueError):
        purge_user(redis_client, bad)
    with pytest.raises(ValueError):
        export_user(redis_client, bad)
