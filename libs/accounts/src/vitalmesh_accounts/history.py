"""A person's recent questions and answers, kept in Postgres (D83).

The run log in Redis expires after an hour: it exists so the page can stream one run. A person
also wants to look back at what they asked yesterday, so the answers that were released to them are
kept here, durably, and nowhere else: no model internals, no operator fields, only what the person
was shown. Rules:

* the writer is the tracker (it already sees every event), so history keeps filling when the API
  is down or the person closed the tab;
* writes are idempotent by correlation id (events are delivered at least once) and a late
  ``question.asked`` never overwrites an answer that already arrived;
* only the latest ``KEEP`` entries per person are kept (the rest are pruned when a new question
  arrives), so the table cannot grow without bound for one person;
* the person can read it, it is part of their data export, and erasing the account deletes it.
  There is no foreign key to the accounts table on purpose: the seeded demo people have no account.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

KEEP = 50
STATUSES = ("asked", "released", "blocked", "needs_review")


@dataclass(frozen=True)
class QaEntry:
    correlation_id: str
    question: str
    status: str
    asked_at: datetime
    answer: dict[str, Any] | None  # the released event's fields: text, partial, claims, citations


class QaHistory(Protocol):
    def record_question(
        self, user_id: str, correlation_id: str, question: str, at: datetime
    ) -> None: ...
    def record_answer(
        self,
        user_id: str,
        correlation_id: str,
        status: str,
        answer: dict[str, Any] | None,
        at: datetime,
    ) -> None: ...
    def recent(self, user_id: str, limit: int = 10) -> list[QaEntry]: ...
    def purge(self, user_id: str) -> int: ...


def _check(status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown history status: {status}")


def qa_export(history: QaHistory, user_id: str) -> list[dict[str, Any]]:
    """Everything kept for one person, for their data export."""
    return [
        {
            "asked_at": e.asked_at.isoformat(),
            "question": e.question,
            "status": e.status,
            "answer": e.answer,
        }
        for e in history.recent(user_id, KEEP)
    ]


class InMemoryQaHistory:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], QaEntry] = {}
        self._lock = threading.Lock()

    def record_question(
        self, user_id: str, correlation_id: str, question: str, at: datetime
    ) -> None:
        with self._lock:
            key = (user_id, correlation_id)
            existing = self._rows.get(key)
            if existing is None:
                self._rows[key] = QaEntry(correlation_id, question, "asked", at, None)
            elif not existing.question:  # the answer arrived first; keep it, add the question
                self._rows[key] = QaEntry(
                    correlation_id, question, existing.status, at, existing.answer
                )
            self._prune(user_id)

    def record_answer(
        self,
        user_id: str,
        correlation_id: str,
        status: str,
        answer: dict[str, Any] | None,
        at: datetime,
    ) -> None:
        _check(status)
        with self._lock:
            key = (user_id, correlation_id)
            existing = self._rows.get(key)
            if existing is not None and existing.status == "released":
                return  # a released answer is final
            asked_at = existing.asked_at if existing else at
            question = existing.question if existing else ""
            self._rows[key] = QaEntry(correlation_id, question, status, asked_at, answer)

    def _prune(self, user_id: str) -> None:
        mine = sorted(
            (e for (u, _), e in self._rows.items() if u == user_id),
            key=lambda e: e.asked_at,
            reverse=True,
        )
        for old in mine[KEEP:]:
            del self._rows[(user_id, old.correlation_id)]

    def recent(self, user_id: str, limit: int = 10) -> list[QaEntry]:
        with self._lock:
            mine = [e for (u, _), e in self._rows.items() if u == user_id]
        return sorted(mine, key=lambda e: e.asked_at, reverse=True)[:limit]

    def purge(self, user_id: str) -> int:
        with self._lock:
            keys = [k for k in self._rows if k[0] == user_id]
            for k in keys:
                del self._rows[k]
        return len(keys)


MIGRATION = """
CREATE TABLE IF NOT EXISTS qa_history (
  user_id text NOT NULL,
  correlation_id text NOT NULL,
  question text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'asked',
  answer jsonb,
  asked_at timestamptz NOT NULL,
  answered_at timestamptz,
  PRIMARY KEY (user_id, correlation_id)
);
CREATE INDEX IF NOT EXISTS qa_history_recent ON qa_history (user_id, asked_at DESC);
"""


class PostgresQaHistory:
    def __init__(self, dsn: str, max_connections: int = 4) -> None:
        self._pool = ConnectionPool(dsn, min_size=1, max_size=max_connections, open=True)
        with self._pool.connection() as conn:
            conn.execute(MIGRATION)

    def close(self) -> None:
        self._pool.close()

    def record_question(
        self, user_id: str, correlation_id: str, question: str, at: datetime
    ) -> None:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                "INSERT INTO qa_history (user_id, correlation_id, question, asked_at) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (user_id, correlation_id) DO UPDATE "
                "SET question = EXCLUDED.question WHERE qa_history.question = ''",
                (user_id, correlation_id, question, at),
            )
            conn.execute(
                "DELETE FROM qa_history WHERE user_id=%s AND correlation_id NOT IN ("
                "SELECT correlation_id FROM qa_history WHERE user_id=%s "
                "ORDER BY asked_at DESC LIMIT %s)",
                (user_id, user_id, KEEP),
            )

    def record_answer(
        self,
        user_id: str,
        correlation_id: str,
        status: str,
        answer: dict[str, Any] | None,
        at: datetime,
    ) -> None:
        _check(status)
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO qa_history (user_id, correlation_id, status, answer, asked_at, "
                "answered_at) VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (user_id, correlation_id) DO UPDATE SET status = EXCLUDED.status, "
                "answer = EXCLUDED.answer, answered_at = EXCLUDED.answered_at "
                "WHERE qa_history.status <> 'released'",
                (user_id, correlation_id, status, Jsonb(answer) if answer else None, at, at),
            )

    def recent(self, user_id: str, limit: int = 10) -> list[QaEntry]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT correlation_id, question, status, asked_at, answer FROM qa_history "
                "WHERE user_id=%s ORDER BY asked_at DESC LIMIT %s",
                (user_id, limit),
            ).fetchall()
        return [QaEntry(r[0], r[1], r[2], r[3], r[4]) for r in rows]

    def purge(self, user_id: str) -> int:
        with self._pool.connection() as conn:
            return conn.execute("DELETE FROM qa_history WHERE user_id=%s", (user_id,)).rowcount
