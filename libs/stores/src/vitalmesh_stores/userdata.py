"""Everything the read models hold about one person: export it, erase it (D75, D89).

The account store (Postgres) owns the account and the profile; the Redis read models hold the
cache of the profile, the lab results, the measurement history, the derived health state, the
reminders and the recent run logs. Privacy rights have to reach all of them: ``export_user``
gathers what is held, ``purge_user`` removes it.

Both find a person's keys through the per-person index (``personkeys``), so their cost depends on
how much THAT person holds and never on how many people exist (D89). The keys whose names are
fixed (profile, labs, state, reminder settings and inbox) are also named directly, so they are
erased even if the index never heard of them (a person's data from before the index existed, until
``scripts/backfill_person_index.py`` has run). A person's id is validated by the caller (it comes
from their session) and again here, and no key or pattern is built from anything else.
"""

from __future__ import annotations

import re
from typing import Any

import redis

from vitalmesh_stores.personkeys import erase, tracked

PROFILE_PREFIX = "t0:profile"
LABS_PREFIX = "labs"
HISTORY_PREFIX = "hist"
RUN_PREFIX = "run"
PENDING_PREFIX = "pending-import"
STATE_PREFIX = "state"
NUDGE_KEYS = ("nudge:prefs", "nudge:inbox")  # per person; the decision cache is shared
_FIXED = (PROFILE_PREFIX, LABS_PREFIX, STATE_PREFIX, *NUDGE_KEYS)
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _decode(raw: Any) -> str:
    return raw.decode() if isinstance(raw, bytes) else str(raw)


def _check(user_id: str) -> None:
    if not _SAFE_ID.match(user_id):
        raise ValueError("unsafe user id")  # never build a key from it


def purge_user(client: redis.Redis, user_id: str) -> int:
    """Delete everything the read models hold about the person; returns the keys removed."""
    _check(user_id)
    removed = erase(client, user_id)  # every indexed key (history series, pending imports, runs)
    fixed = [f"{prefix}:{user_id}" for prefix in _FIXED]
    return removed + int(client.delete(*fixed))  # keys already gone count zero: no double count


def export_user(client: redis.Redis, user_id: str) -> dict[str, Any]:
    """What the read models hold about the person (the profile and account come from Postgres)."""
    _check(user_id)
    keys = set(tracked(client, user_id)) | {f"{prefix}:{user_id}" for prefix in _FIXED}
    labs = [_decode(v) for v in client.hgetall(f"{LABS_PREFIX}:{user_id}").values()]
    history: dict[str, list[str]] = {}
    for key in sorted(keys):
        if key.startswith(f"{HISTORY_PREFIX}:{user_id}:"):
            members = [_decode(m) for m in client.zrange(key, 0, -1)]
            if members:
                history[key.rsplit(":", 1)[1]] = members
    state = client.get(f"{STATE_PREFIX}:{user_id}")
    nudges = {
        "settings": {
            (_decode(k)): _decode(v) for k, v in client.hgetall(f"nudge:prefs:{user_id}").items()
        },
        "inbox": [_decode(v) for v in client.hgetall(f"nudge:inbox:{user_id}").values()],
    }
    return {
        "labs": labs,
        "history": history,
        "nudges": nudges,
        "derived_state": _decode(state) if state is not None else None,
    }
