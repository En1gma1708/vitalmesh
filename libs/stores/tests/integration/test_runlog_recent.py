from __future__ import annotations

import time
import uuid

import pytest
import redis
from vitalmesh_stores import RedisRunLog

pytestmark = pytest.mark.integration


@pytest.fixture
def client() -> redis.Redis:
    c = redis.Redis.from_url("redis://127.0.0.1:6379/0")
    try:
        c.ping()
    except redis.ConnectionError:
        pytest.skip("Redis is not running")
    return c


def test_recent_runs_newest_first_and_expired_ones_drop_out(client: redis.Redis) -> None:
    prefix = f"runtest-{uuid.uuid4().hex[:8]}"
    log = RedisRunLog(client, prefix=prefix, ttl_s=3600)
    for cid in ("a", "b", "c"):
        log.append(cid, "u-1", {"event_id": f"{cid}-1", "type": "question.asked", "at": "x"})
        time.sleep(0.01)  # distinct first-seen times
    log.append("a", "u-1", {"event_id": "a-2", "type": "run.planned", "at": "y"})  # not 'newer'
    assert [cid for cid, _ in log.recent(10)] == ["c", "b", "a"]
    assert len(log.recent(2)) == 2
    assert [len(rec.entries) for _, rec in log.recent(10)] == [1, 1, 2]
    client.zadd(f"{prefix}:index", {"ghost": time.time() - 7200})  # older than the ttl
    assert "ghost" not in [cid for cid, _ in log.recent(10)]
    client.delete(*client.keys(f"{prefix}:*"))
