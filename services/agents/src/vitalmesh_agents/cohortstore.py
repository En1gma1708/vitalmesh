"""Where the T1 cohort templates live, and the counters that say whether the cache pays (D70).

Redis in production (a cache, never the source of truth: a lost entry is only a miss), a dict in
tests and the in-process bus. Entries expire, so a template that nobody invalidated still ages
out; the rules version in the key (cohort.cache_key) handles the deliberate invalidation.
"""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

import redis

from vitalmesh_agents.cohort import Template, TemplateClaim

ENTRY_TTL_S = 24 * 3600
STAT_NAMES = ("hit", "miss", "fill", "uncacheable", "personalise_failed")


@dataclass(frozen=True)
class CohortEntry:
    template: Template
    citations: tuple[bytes, ...] = field(default_factory=tuple)  # serialised Citation messages


class CohortStore(Protocol):
    def get(self, key: str) -> CohortEntry | None: ...

    def put(self, key: str, entry: CohortEntry) -> None: ...

    def count(self, stat: str) -> None: ...

    def stats(self) -> dict[str, int]: ...


def encode(entry: CohortEntry) -> str:
    return json.dumps(
        {
            "text": entry.template.text,
            "claims": [asdict(c) for c in entry.template.claims],
            "citations": [base64.b64encode(c).decode() for c in entry.citations],
        }
    )


def decode(raw: str) -> CohortEntry:
    data: dict[str, Any] = json.loads(raw)
    return CohortEntry(
        template=Template(
            text=data["text"], claims=tuple(TemplateClaim(**c) for c in data["claims"])
        ),
        citations=tuple(base64.b64decode(c) for c in data["citations"]),
    )


class InMemoryCohortStore:
    def __init__(self) -> None:
        self._entries: dict[str, CohortEntry] = {}
        self._stats: dict[str, int] = dict.fromkeys(STAT_NAMES, 0)

    def get(self, key: str) -> CohortEntry | None:
        return self._entries.get(key)

    def put(self, key: str, entry: CohortEntry) -> None:
        self._entries[key] = entry

    def count(self, stat: str) -> None:
        self._stats[stat] = self._stats.get(stat, 0) + 1

    def stats(self) -> dict[str, int]:
        return dict(self._stats)


class RedisCohortStore:
    def __init__(self, client: redis.Redis, ttl_s: int = ENTRY_TTL_S) -> None:
        self._r = client
        self._ttl = ttl_s

    def get(self, key: str) -> CohortEntry | None:
        raw = self._r.get(key)
        if raw is None:
            return None
        try:
            return decode(raw.decode() if isinstance(raw, bytes) else str(raw))
        except (ValueError, KeyError, TypeError):
            return None  # a corrupt entry is a miss, never an error in the user's path

    def put(self, key: str, entry: CohortEntry) -> None:
        self._r.set(key, encode(entry), ex=self._ttl)

    def count(self, stat: str) -> None:
        self._r.hincrby("cohort:stats", stat, 1)

    def stats(self) -> dict[str, int]:
        raw = self._r.hgetall("cohort:stats")
        out = dict.fromkeys(STAT_NAMES, 0)
        for k, v in raw.items():
            out[k.decode() if isinstance(k, bytes) else str(k)] = int(v)
        return out
