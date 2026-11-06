"""A per-person index of the Redis keys that hold that person's data (D89).

Why: erasing or exporting one person used to find their keys with SCAN, which walks the WHOLE
keyspace: one pass over 13,500 keys took about 14 s with the default batch size (measured), so the
cost grew with the number of people, not with the size of the one person's data. It also made it
easy to forget a key: ``state:<user>`` was never erased (found 2026-11-07). Now every store that
writes a key about a person first records the key's name in one small set, ``pidx:<user>``.
Erase and export read that set: the cost depends on how much that person holds, never on how many
people exist.

Rules that keep it honest:
* the index entry is written BEFORE the data, in the same pipeline: a crash between the two
  leaves an index entry for a key that does not exist, which is harmless; the opposite order
  could leave data nobody can find;
* the index has no expiry (an expiring index would orphan keys that outlive it); ``erase``
  removes it, and entries for keys that expired by themselves are removed too when erased;
* key names are NOT changed (no migration): ``backfill`` adds existing keys to the index once;
* keys are single-key operations, so the same code works on a sharded Redis (Redis Cluster).
  The index and a person's keys may sit on different shards: erase deletes key by key.

A new store that writes a per-person key must call ``track``. ``test_person_index`` scans the whole
keyspace after exercising every store and fails if a key that names the person is not indexed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import redis

INDEX_PREFIX = "pidx"
BATCH = 500
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def index_key(user_id: str) -> str:
    if not _SAFE_ID.match(user_id):
        raise ValueError("unsafe user id")  # never build a key from it
    return f"{INDEX_PREFIX}:{user_id}"


def track(client: Any, user_id: str, *keys: str) -> None:
    """Record that these keys hold the person's data. ``client`` may be a pipeline: call this
    BEFORE the data write so both travel together."""
    if keys:
        client.sadd(index_key(user_id), *keys)


def untrack(client: Any, user_id: str, *keys: str) -> None:
    if keys:
        client.srem(index_key(user_id), *keys)


def tracked(client: redis.Redis, user_id: str) -> list[str]:
    return sorted(_text(k) for k in client.smembers(index_key(user_id)))


def erase(client: redis.Redis, user_id: str) -> int:
    """Delete every indexed key and then the index; returns the number of data keys that existed."""
    index = index_key(user_id)
    removed = 0
    keys = [_text(k) for k in client.smembers(index)]
    for start in range(0, len(keys), BATCH):
        removed += int(client.delete(*keys[start : start + BATCH]))
    client.delete(index)
    return removed


def backfill(client: redis.Redis, entries: Iterable[tuple[str, str]]) -> int:
    """Add (user_id, key) pairs to the index; idempotent, never deletes or renames anything."""
    added = 0
    pipe = client.pipeline()
    for n, (user_id, key) in enumerate(entries, start=1):
        pipe.sadd(index_key(user_id), key)
        added += 1
        if n % 1000 == 0:
            pipe.execute()
            pipe = client.pipeline()
    pipe.execute()
    return added


def _text(raw: Any) -> str:
    return raw.decode() if isinstance(raw, bytes) else str(raw)
