"""The per-person key index (D89): complete, scan-free, and honest about legacy keys."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis
from vitalmesh_processor.state import HealthState, RedisStateStore
from vitalmesh_stores import (
    Item,
    Point,
    Prefs,
    ProfileRecord,
    RedisHistoryStore,
    RedisLabStore,
    RedisNudgeStore,
    RedisPendingImports,
    RedisProfileStore,
    RedisRunLog,
    RedisWorkoutLog,
    backfill,
    export_user,
    index_key,
    purge_user,
    tracked,
)
from vitalmesh_wellness.labs import interpret
from vitalmesh_wellness.synthetic import synthetic_user

pytestmark = pytest.mark.integration


class NoScan(redis.Redis):
    """A client that refuses to walk the keyspace: erase and export must not need it."""

    def scan_iter(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("SCAN is not allowed here")

    def scan(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("SCAN is not allowed here")

    def keys(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("KEYS is not allowed here")


@pytest.fixture
def client() -> redis.Redis:
    c = NoScan.from_url("redis://127.0.0.1:6379/0")
    try:
        c.ping()
    except redis.ConnectionError:
        pytest.skip("Redis is not running")
    return c


def uid() -> str:
    return f"t-{uuid.uuid4().hex[:10]}"


def populate(c: redis.Redis, user: str) -> None:
    """Use EVERY Redis store that writes something about a person."""
    now = datetime.now(UTC)
    profile, sleep = synthetic_user(3, 1)
    RedisProfileStore(c).put(
        ProfileRecord(
            profile=profile.model_copy(update={"user_id": user}), recent_sleep_hours=tuple(sleep)
        )
    )
    RedisLabStore(c).put(
        user, interpret("LDL Cholesterol", now, value=128, unit="mg/dL", reference_text="< 100")
    )
    hist = RedisHistoryStore(c)
    hist.add(user, "body_weight", Point(now, 70.0, "kg"))
    hist.add(user, "step_count", Point(now - timedelta(days=1), 5000.0, "count"))
    RedisPendingImports(c).put(user, "abcdefgh1234", {"source": "csv", "readings": []})
    nudges = RedisNudgeStore(c)
    nudges.set_prefs(user, Prefs(True, "UTC"))
    nudges.add(user, Item("n-1", "log_reminder", "gentle", "t", now))
    RedisStateStore(c).put(HealthState(user_id=user))
    RedisRunLog(c).append(f"run-{user}", user, {"event_id": f"e-{user}", "type": "question.asked"})
    RedisWorkoutLog(c).record(user, now.date(), "push")


def now_day() -> str:
    return datetime.now(UTC).date().isoformat()


def keys_naming(c: redis.Redis, user: str) -> set[str]:
    """Every key in Redis that mentions the person (a test may scan; the product may not)."""
    plain = redis.Redis.from_url("redis://127.0.0.1:6379/0")
    found = {k.decode() for k in plain.scan_iter(match=f"*{user}*", count=1000)}
    return found


def test_every_store_registers_what_it_writes_and_erase_leaves_nothing(client: redis.Redis) -> None:
    a, b = uid(), uid()
    populate(client, a)
    populate(client, b)
    try:
        written = keys_naming(client, a) - {index_key(a)}
        assert (
            len(written) >= 10
        )  # profile, labs, 2 history, pending, 2 nudge, state, run, workouts
        missing = written - set(tracked(client, a))
        # the run log's per-run keys are named by correlation id, which here embeds the user id
        assert missing == set(), f"keys that name the person but are not in the index: {missing}"
        held = export_user(client, a)
        assert held["suggested_workouts"] == {now_day(): "push"}
        assert held["derived_state"] is not None and set(held["history"]) == {
            "body_weight",
            "step_count",
        }
        assert purge_user(client, a) >= len(written)
        assert keys_naming(client, a) == set()  # nothing at all mentions the person
        assert keys_naming(client, b) != set()  # the other person is untouched
        assert export_user(client, a)["derived_state"] is None
    finally:
        purge_user(client, a)
        purge_user(client, b)


def test_erase_and_export_never_walk_the_keyspace(client: redis.Redis) -> None:
    user = uid()
    populate(client, user)
    try:
        export_user(client, user)  # NoScan raises if either touches SCAN or KEYS
        assert purge_user(client, user) > 0
    finally:
        purge_user(client, user)


def test_the_state_key_that_used_to_survive_erasure_is_now_erased(client: redis.Redis) -> None:
    user = uid()
    RedisStateStore(client).put(HealthState(user_id=user))
    assert client.exists(f"state:{user}") == 1
    purge_user(client, user)
    assert client.exists(f"state:{user}") == 0


def test_a_key_that_expired_by_itself_is_harmless_in_the_index(client: redis.Redis) -> None:
    user = uid()
    RedisPendingImports(client, ttl_s=1).put(user, "abcdefgh1234", {"readings": []})
    client.delete(f"pending-import:{user}:abcdefgh1234")  # as if the TTL had fired
    assert purge_user(client, user) == 0 and tracked(client, user) == []


def test_deleting_a_pending_import_removes_it_from_the_index(client: redis.Redis) -> None:
    user = uid()
    pending = RedisPendingImports(client)
    pending.put(user, "abcdefgh1234", {"readings": []})
    assert tracked(client, user) == [f"pending-import:{user}:abcdefgh1234"]
    pending.delete(user, "abcdefgh1234")
    assert tracked(client, user) == []
    purge_user(client, user)


def test_legacy_keys_are_erased_only_after_the_backfill_unless_their_name_is_fixed(
    client: redis.Redis,
) -> None:
    user = uid()
    legacy_hist, legacy_state = f"hist:{user}:body_weight", f"state:{user}"
    client.zadd(legacy_hist, {"1|70.0|kg": 1})  # written before the index existed
    client.set(legacy_state, "{}")
    assert tracked(client, user) == []
    try:
        purge_user(client, user)
        assert client.exists(legacy_state) == 0  # a fixed name is erased without the index
        assert client.exists(legacy_hist) == 1  # a variable name needs the index: the backfill
        assert backfill(client, [(user, legacy_hist)]) == 1
        assert backfill(client, [(user, legacy_hist)]) == 1  # idempotent: same set, no duplicate
        assert tracked(client, user) == [legacy_hist]
        purge_user(client, user)
        assert client.exists(legacy_hist) == 0
    finally:
        client.delete(legacy_hist, legacy_state, index_key(user))


def test_an_unsafe_id_never_builds_a_key(client: redis.Redis) -> None:
    for bad in ("", "a*", "u-1:../x", "x" * 65, "u 1"):
        with pytest.raises(ValueError):
            purge_user(client, bad)
        with pytest.raises(ValueError):
            index_key(bad)


def test_the_workout_log_keeps_fourteen_days_and_is_erased(client: redis.Redis) -> None:
    from datetime import date

    user = uid()
    log = RedisWorkoutLog(client)
    first = date(2026, 11, 1)
    try:
        for i in range(20):
            log.record(user, first + timedelta(days=i), "push" if i % 2 else "pull")
        kept = log.recent(user)
        assert len(kept) == 14
        assert min(kept) == first + timedelta(days=6) and max(kept) == first + timedelta(days=19)
        assert f"workoutlog:{user}" in tracked(client, user)
        assert client.ttl(f"workoutlog:{user}") > 0
        client.hset(f"workoutlog:{user}", "not-a-date", "push")  # garbage is ignored, not trusted
        assert len(log.recent(user)) == 14
    finally:
        purge_user(client, user)
    assert log.recent(user) == {}
