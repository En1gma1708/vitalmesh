"""What the Engagement agent and Delivery need to remember about one person (D87).

* ``Prefs``: nudges are OFF until the person turns them on, and the browser's timezone is stored
  with the switch so quiet hours are the person's own hours (the profile has no timezone).
* the inbox: every nudge that was delivered, newest first, at most ``MAX_ITEMS``; a dismissal is
  kept (a person who dismisses a kind of nudge should hear less of it). Items live 30 days.
* decision cache and counters: shared across people on purpose (a decision is cached by the
  SITUATION, never by the person; see ``vitalmesh_agents.engagement``).

Redis has an in-memory twin for tests; both satisfy ``nudge_contract`` in the tests.
``purge_user`` and ``export_user`` reach the per-person keys.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

import redis

MAX_ITEMS = 50
ITEM_TTL_S = 30 * 24 * 3600
DECISION_TTL_S = 24 * 3600
PREFS_PREFIX = "nudge:prefs"
INBOX_PREFIX = "nudge:inbox"
STATS_KEY = "nudge:stats"
DECISION_PREFIX = "nudge:dec"


@dataclass(frozen=True)
class Prefs:
    enabled: bool = False
    timezone: str = "UTC"


@dataclass(frozen=True)
class Item:
    id: str
    kind: str
    tone: str
    text: str
    at: datetime
    dismissed: bool = False


class NudgeStore(Protocol):
    def get_prefs(self, user_id: str) -> Prefs: ...

    def set_prefs(self, user_id: str, prefs: Prefs) -> None: ...

    def add(self, user_id: str, item: Item) -> bool:
        """False when an item with this id is already there (a redelivered event)."""

    def items(self, user_id: str) -> list[Item]:
        """Newest first."""

    def dismiss(self, user_id: str, item_id: str) -> bool: ...

    def stat(self, name: str, n: int = 1) -> None: ...

    def stats(self) -> dict[str, int]: ...

    def decision(self, key: str) -> str | None: ...

    def remember_decision(self, key: str, value: str) -> None: ...


def _trim(items: list[Item]) -> list[Item]:
    return sorted(items, key=lambda i: i.at, reverse=True)[:MAX_ITEMS]


def _dump(item: Item) -> str:
    return json.dumps(
        {
            "id": item.id,
            "kind": item.kind,
            "tone": item.tone,
            "text": item.text,
            "at": item.at.astimezone(UTC).isoformat(),
            "dismissed": item.dismissed,
        }
    )


def _load(raw: bytes | str) -> Item:
    d = json.loads(raw)
    return Item(
        d["id"], d["kind"], d["tone"], d["text"], datetime.fromisoformat(d["at"]), d["dismissed"]
    )


class InMemoryNudgeStore:
    def __init__(self) -> None:
        self._prefs: dict[str, Prefs] = {}
        self._items: dict[str, dict[str, Item]] = {}
        self._stats: dict[str, int] = {}
        self._decisions: dict[str, str] = {}

    def get_prefs(self, user_id: str) -> Prefs:
        return self._prefs.get(user_id, Prefs())

    def set_prefs(self, user_id: str, prefs: Prefs) -> None:
        self._prefs[user_id] = prefs

    def add(self, user_id: str, item: Item) -> bool:
        mine = self._items.setdefault(user_id, {})
        if item.id in mine:
            return False
        mine[item.id] = item
        self._items[user_id] = {i.id: i for i in _trim(list(mine.values()))}
        return True

    def items(self, user_id: str) -> list[Item]:
        return _trim(list(self._items.get(user_id, {}).values()))

    def dismiss(self, user_id: str, item_id: str) -> bool:
        mine = self._items.get(user_id, {})
        if item_id not in mine:
            return False
        mine[item_id] = replace(mine[item_id], dismissed=True)
        return True

    def stat(self, name: str, n: int = 1) -> None:
        self._stats[name] = self._stats.get(name, 0) + n

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def decision(self, key: str) -> str | None:
        return self._decisions.get(key)

    def remember_decision(self, key: str, value: str) -> None:
        self._decisions[key] = value


class RedisNudgeStore:
    def __init__(self, client: redis.Redis) -> None:
        self._r = client

    def get_prefs(self, user_id: str) -> Prefs:
        raw = self._r.hgetall(f"{PREFS_PREFIX}:{user_id}")
        if not raw:
            return Prefs()
        data = {(k.decode() if isinstance(k, bytes) else k): v for k, v in raw.items()}
        tz = data.get("timezone", b"UTC")
        return Prefs(
            enabled=data.get("enabled") in (b"1", "1"),
            timezone=tz.decode() if isinstance(tz, bytes) else str(tz),
        )

    def set_prefs(self, user_id: str, prefs: Prefs) -> None:
        key = f"{PREFS_PREFIX}:{user_id}"
        self._r.hset(
            key, mapping={"enabled": "1" if prefs.enabled else "0", "timezone": prefs.timezone}
        )

    def add(self, user_id: str, item: Item) -> bool:
        key = f"{INBOX_PREFIX}:{user_id}"
        if not self._r.hsetnx(key, item.id, _dump(item)):
            return False
        mine = self.items(user_id)
        keep = {i.id for i in mine}
        stale = [
            f for f in self._r.hkeys(key) if (f.decode() if isinstance(f, bytes) else f) not in keep
        ]
        if stale:
            self._r.hdel(key, *stale)
        self._r.expire(key, ITEM_TTL_S)
        return True

    def items(self, user_id: str) -> list[Item]:
        raw = self._r.hgetall(f"{INBOX_PREFIX}:{user_id}")
        return _trim([_load(v) for v in raw.values()])

    def dismiss(self, user_id: str, item_id: str) -> bool:
        key = f"{INBOX_PREFIX}:{user_id}"
        raw = self._r.hget(key, item_id)
        if raw is None:
            return False
        self._r.hset(key, item_id, _dump(replace(_load(raw), dismissed=True)))
        return True

    def stat(self, name: str, n: int = 1) -> None:
        self._r.hincrby(STATS_KEY, name, n)

    def stats(self) -> dict[str, int]:
        return {
            (k.decode() if isinstance(k, bytes) else k): int(v)
            for k, v in self._r.hgetall(STATS_KEY).items()
        }

    def decision(self, key: str) -> str | None:
        raw = self._r.get(f"{DECISION_PREFIX}:{key}")
        return None if raw is None else (raw.decode() if isinstance(raw, bytes) else str(raw))

    def remember_decision(self, key: str, value: str) -> None:
        self._r.set(f"{DECISION_PREFIX}:{key}", value, ex=DECISION_TTL_S)
