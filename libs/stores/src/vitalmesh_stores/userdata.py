"""Everything the read models hold about one person: export it, erase it (D75).

The account store (Postgres) owns the account and the profile; the Redis read models hold the
cache of the profile, the lab results, the measurement history and the recent run logs. Privacy
rights have to reach all of them: ``export_user`` gathers what is held, ``purge_user`` removes it.
Keys are the stores' own prefixes; a person's id is validated by the caller (it comes from their
session), and the scan pattern contains no wildcard from user input.
"""

from __future__ import annotations

import re
from typing import Any

import redis

PROFILE_PREFIX = "t0:profile"
LABS_PREFIX = "labs"
HISTORY_PREFIX = "hist"
RUN_PREFIX = "run"
PENDING_PREFIX = "pending-import"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _decode(raw: Any) -> str:
    return raw.decode() if isinstance(raw, bytes) else str(raw)


def _check(user_id: str) -> None:
    if not _SAFE_ID.match(user_id):
        raise ValueError("unsafe user id")  # never build a key or a scan pattern from it


def purge_user(client: redis.Redis, user_id: str) -> int:
    """Delete the person's cached profile, labs and history; returns the number of keys removed."""
    _check(user_id)
    keys = [f"{PROFILE_PREFIX}:{user_id}", f"{LABS_PREFIX}:{user_id}"]
    keys += [_decode(k) for k in client.scan_iter(match=f"{HISTORY_PREFIX}:{user_id}:*")]
    keys += [_decode(k) for k in client.scan_iter(match=f"{PENDING_PREFIX}:{user_id}:*")]
    return int(client.delete(*keys)) if keys else 0


def export_user(client: redis.Redis, user_id: str) -> dict[str, Any]:
    """Labs and measurement history as stored (the profile and account come from Postgres)."""
    _check(user_id)
    labs = [_decode(v) for v in client.hgetall(f"{LABS_PREFIX}:{user_id}").values()]
    history: dict[str, list[str]] = {}
    for raw_key in client.scan_iter(match=f"{HISTORY_PREFIX}:{user_id}:*"):
        key = _decode(raw_key)
        metric = key.rsplit(":", 1)[1]
        history[metric] = [_decode(m) for m in client.zrange(key, 0, -1)]
    return {"labs": labs, "history": history}
