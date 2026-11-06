from __future__ import annotations

import uuid

import pytest
import redis
from nudge_contract import check_contract
from vitalmesh_stores import RedisNudgeStore, export_user, purge_user

pytestmark = pytest.mark.integration


@pytest.fixture
def client() -> redis.Redis:
    c = redis.Redis.from_url("redis://127.0.0.1:6379/0")
    try:
        c.ping()
    except redis.ConnectionError:
        pytest.skip("Redis is not running")
    return c


def test_the_redis_nudge_store_meets_the_contract_and_is_exported_and_erased(
    client: redis.Redis,
) -> None:
    a, b = f"t-{uuid.uuid4().hex[:8]}", f"t-{uuid.uuid4().hex[:8]}"
    store = RedisNudgeStore(client)
    try:
        check_contract(store, a, b)
        held = export_user(client, a)["nudges"]
        assert held["settings"]["enabled"] == "1" and len(held["inbox"]) == 50
        store.set_prefs(b, store.get_prefs(a))
        assert purge_user(client, a) >= 2  # settings and inbox
        assert export_user(client, a)["nudges"] == {"settings": {}, "inbox": []}
        assert store.get_prefs(a).enabled is False
        assert store.get_prefs(b).enabled is True  # the other person is untouched
    finally:
        purge_user(client, a)
        purge_user(client, b)
        client.hdel("nudge:stats", "x_test")
        client.delete("nudge:dec:k")
