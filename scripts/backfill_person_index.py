"""Add the Redis keys that already exist to the per-person index (D89). Run once.

    uv run python scripts/backfill_person_index.py            # dry run: counts only
    uv run python scripts/backfill_person_index.py --apply    # adds index entries

It only ADDS entries to ``pidx:<user>`` sets: it never renames, rewrites or deletes a data key, so
running it twice, or not at all, cannot lose data. Until it has run, erasing a person still removes
the keys whose names are fixed (profile, labs, state, reminders) but not their history series or
pending imports, because those names are only known from the index. It walks the keyspace
ONCE
(that is what the index exists to avoid doing again).
"""

from __future__ import annotations

import os
import re
import sys
from collections import Counter

import redis
from vitalmesh_stores import backfill

SAFE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# prefix -> number of ':'-separated parts of the prefix (the user id is the next part)
PREFIXES = {
    "t0:profile": 2,
    "labs": 1,
    "hist": 1,
    "pending-import": 1,
    "nudge:prefs": 2,
    "nudge:inbox": 2,
    "state": 1,
}


def attribute(key: str) -> tuple[str, str] | None:
    """(user_id, key) for a key this script knows, else None. Never guesses."""
    for prefix, depth in PREFIXES.items():
        if key.startswith(prefix + ":"):
            parts = key.split(":")
            if len(parts) <= depth:
                return None
            user = parts[depth]
            return (user, key) if SAFE.match(user) else None
    return None


def main() -> None:
    apply = "--apply" in sys.argv
    client = redis.Redis.from_url(os.environ.get("VITALMESH_REDIS_URL", "redis://127.0.0.1:6379/0"))
    found: list[tuple[str, str]] = []
    seen = Counter[str]()
    skipped = 0
    total = 0
    for raw in client.scan_iter(count=1000):
        total += 1
        key = raw.decode() if isinstance(raw, bytes) else str(raw)
        got = attribute(key)
        if got is None:
            if any(key.startswith(p + ":") for p in PREFIXES):
                skipped += 1  # looks like ours but the id is not a safe id: leave it alone
            continue
        found.append(got)
        seen[
            key.split(":")[0]
            if not key.startswith(("t0:", "nudge:"))
            else ":".join(key.split(":")[:2])
        ] += 1
    users = {u for u, _ in found}
    print(f"keys scanned: {total}; person keys found: {len(found)} for {len(users)} people")
    for prefix, n in sorted(seen.items()):
        print(f"  {prefix:<16} {n}")
    if skipped:
        print(f"  skipped (unsafe id): {skipped}")
    if not apply:
        print("dry run: nothing changed. Re-run with --apply to add the index entries.")
        return
    print(f"index entries added (idempotent): {backfill(client, found)}")


if __name__ == "__main__":
    main()
