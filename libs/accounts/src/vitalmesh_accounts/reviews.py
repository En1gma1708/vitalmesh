"""The clinician's review queue: held answers waiting for a human, and what was decided (D85).

When the system will not release an answer on its own (a marked lab value, a risky question, the
independent checker still unhappy after one rewrite), the run pauses and a ``review.requested``
event is published. This store keeps those cases durably, so a clinician can decide hours later (the
run log expires in an hour, a pending review must not). Rules:

* the writer is the tracker: it records ``review.requested`` (the case) and ``review.decided`` (the
  outcome, whoever made it: the console or the command-line stand-in), so the queue and the
  pipeline cannot disagree;
* idempotent and order-independent: events are delivered at least once and across topics in no
  fixed order, so a decision that arrives before its request leaves a stub that the request later
  fills in; a decided case never goes back to pending;
* the person's id is stored (so account deletion can erase their cases) but is NEVER returned to a
  clinician: a case is identified by its recommendation id alone;
* a case holds health content (the question, the draft, the reasons): it is readable only through
  the clinician routes, appears in the person's own data export, and is erased with the account.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"
DECISIONS = (APPROVED, REJECTED)


@dataclass(frozen=True)
class ReviewCase:
    recommendation_id: str
    correlation_id: str
    requested_at: datetime
    question: str
    briefing: str
    reasons: list[str]
    draft: dict[str, Any]  # text, risk, partial, claims [{text, basis}], citations [{...}]
    status: str = PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    note: str = ""
    has_detail: bool = field(default=True, repr=False)


class ReviewQueue(Protocol):
    def put_request(self, user_id: str, case: ReviewCase) -> None: ...
    def record_decision(
        self,
        recommendation_id: str,
        decision: str,
        reviewer_id: str,
        at: datetime,
        note: str,
        user_id: str = "",
    ) -> bool: ...
    def get(self, recommendation_id: str) -> ReviewCase | None: ...
    def cases(self, status: str, limit: int = 50) -> list[ReviewCase]: ...
    def owner(self, recommendation_id: str) -> str | None: ...
    def for_user(self, user_id: str) -> list[ReviewCase]: ...
    def purge(self, user_id: str) -> int: ...


def review_export(queue: ReviewQueue, user_id: str) -> list[dict[str, Any]]:
    """The person's own held answers and outcomes, for their data export."""
    return [
        {
            "requested_at": c.requested_at.isoformat(),
            "question": c.question,
            "status": c.status,
            "decided_at": c.decided_at.isoformat() if c.decided_at else None,
            "note": c.note,
        }
        for c in queue.for_user(user_id)
    ]


def _check(decision: str) -> None:
    if decision not in DECISIONS:
        raise ValueError(f"unknown review decision: {decision}")


class InMemoryReviewQueue:
    def __init__(self) -> None:
        self._cases: dict[str, ReviewCase] = {}
        self._owner: dict[str, str] = {}
        self._lock = threading.Lock()

    def put_request(self, user_id: str, case: ReviewCase) -> None:
        with self._lock:
            existing = self._cases.get(case.recommendation_id)
            self._owner[case.recommendation_id] = user_id
            if existing is None:
                self._cases[case.recommendation_id] = case
            elif not existing.has_detail:  # the decision arrived first: keep it, add the detail
                self._cases[case.recommendation_id] = replace(
                    case,
                    status=existing.status,
                    decided_by=existing.decided_by,
                    decided_at=existing.decided_at,
                    note=existing.note,
                )

    def record_decision(
        self,
        recommendation_id: str,
        decision: str,
        reviewer_id: str,
        at: datetime,
        note: str,
        user_id: str = "",
    ) -> bool:
        """True if this call decided the case; False if it was already decided."""
        _check(decision)
        with self._lock:
            existing = self._cases.get(recommendation_id)
            if existing is not None and existing.status != PENDING:
                return False
            if existing is None:
                if user_id:
                    self._owner[recommendation_id] = user_id
                existing = ReviewCase(recommendation_id, "", at, "", "", [], {}, has_detail=False)
            self._cases[recommendation_id] = replace(
                existing, status=decision, decided_by=reviewer_id, decided_at=at, note=note
            )
            return True

    def get(self, recommendation_id: str) -> ReviewCase | None:
        with self._lock:
            return self._cases.get(recommendation_id)

    def cases(self, status: str, limit: int = 50) -> list[ReviewCase]:
        with self._lock:
            rows = [c for c in self._cases.values() if c.has_detail]
        wanted = [c for c in rows if (c.status == PENDING) == (status == PENDING)]
        key = (
            (lambda c: c.requested_at)
            if status == PENDING
            else (lambda c: c.decided_at or c.requested_at)
        )
        # pending: oldest first (longest waiting); decided: newest first
        return sorted(wanted, key=key, reverse=status != PENDING)[:limit]

    def owner(self, recommendation_id: str) -> str | None:
        with self._lock:
            return self._owner.get(recommendation_id)

    def for_user(self, user_id: str) -> list[ReviewCase]:
        with self._lock:
            return [
                c for r, c in self._cases.items() if self._owner.get(r) == user_id and c.has_detail
            ]

    def purge(self, user_id: str) -> int:
        with self._lock:
            ids = [r for r, u in self._owner.items() if u == user_id]
            for r in ids:
                self._cases.pop(r, None)
                del self._owner[r]
        return len(ids)


MIGRATION = """
CREATE TABLE IF NOT EXISTS reviews (
  recommendation_id text PRIMARY KEY,
  user_id text NOT NULL DEFAULT '',
  correlation_id text NOT NULL DEFAULT '',
  requested_at timestamptz NOT NULL,
  question text NOT NULL DEFAULT '',
  briefing text NOT NULL DEFAULT '',
  reasons jsonb NOT NULL DEFAULT '[]',
  draft jsonb NOT NULL DEFAULT '{}',
  has_detail boolean NOT NULL DEFAULT false,
  status text NOT NULL DEFAULT 'pending',
  decided_by text,
  decided_at timestamptz,
  note text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS reviews_status ON reviews (status, requested_at);
CREATE INDEX IF NOT EXISTS reviews_user ON reviews (user_id);
"""
_COLS = (
    "recommendation_id, correlation_id, requested_at, question, briefing, reasons, draft, "
    "status, decided_by, decided_at, note, has_detail"
)


def _case(r: Any) -> ReviewCase:
    return ReviewCase(
        r[0], r[1], r[2], r[3], r[4], list(r[5]), dict(r[6]), r[7], r[8], r[9], r[10], bool(r[11])
    )


class PostgresReviewQueue:
    def __init__(self, dsn: str, max_connections: int = 4) -> None:
        self._pool = ConnectionPool(dsn, min_size=1, max_size=max_connections, open=True)
        with self._pool.connection() as conn:
            conn.execute(MIGRATION)

    def close(self) -> None:
        self._pool.close()

    def put_request(self, user_id: str, case: ReviewCase) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO reviews (recommendation_id, user_id, correlation_id, requested_at, "
                "question, briefing, reasons, draft, has_detail) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,true) "
                "ON CONFLICT (recommendation_id) DO UPDATE SET user_id = EXCLUDED.user_id, "
                "correlation_id = EXCLUDED.correlation_id, requested_at = EXCLUDED.requested_at, "
                "question = EXCLUDED.question, briefing = EXCLUDED.briefing, "
                "reasons = EXCLUDED.reasons, draft = EXCLUDED.draft, has_detail = true "
                "WHERE reviews.has_detail = false",
                (
                    case.recommendation_id,
                    user_id,
                    case.correlation_id,
                    case.requested_at,
                    case.question,
                    case.briefing,
                    Jsonb(case.reasons),
                    Jsonb(case.draft),
                ),
            )

    def record_decision(
        self,
        recommendation_id: str,
        decision: str,
        reviewer_id: str,
        at: datetime,
        note: str,
        user_id: str = "",
    ) -> bool:
        _check(decision)
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO reviews (recommendation_id, user_id, requested_at, status, "
                "decided_by, decided_at, note) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (recommendation_id) DO UPDATE SET status = EXCLUDED.status, "
                "decided_by = EXCLUDED.decided_by, decided_at = EXCLUDED.decided_at, "
                "note = EXCLUDED.note WHERE reviews.status = 'pending' "
                "RETURNING recommendation_id",
                (recommendation_id, user_id, at, decision, reviewer_id, at, note),
            ).fetchone()
        return row is not None

    def get(self, recommendation_id: str) -> ReviewCase | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLS} FROM reviews WHERE recommendation_id=%s",  # nosec B608 - constant columns, bound values
                (recommendation_id,),
            ).fetchone()
        return _case(row) if row else None

    def cases(self, status: str, limit: int = 50) -> list[ReviewCase]:
        pending = status == PENDING
        order = "requested_at ASC" if pending else "decided_at DESC"
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLS} FROM reviews WHERE has_detail AND (status = 'pending') = %s "  # nosec B608 - constant columns and order
                f"ORDER BY {order} LIMIT %s",
                (pending, limit),
            ).fetchall()
        return [_case(r) for r in rows]

    def owner(self, recommendation_id: str) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT user_id FROM reviews WHERE recommendation_id=%s", (recommendation_id,)
            ).fetchone()
        return (row[0] or None) if row else None

    def for_user(self, user_id: str) -> list[ReviewCase]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLS} FROM reviews WHERE user_id=%s AND has_detail "  # nosec B608 - constant columns
                "ORDER BY requested_at DESC",
                (user_id,),
            ).fetchall()
        return [_case(r) for r in rows]

    def purge(self, user_id: str) -> int:
        with self._pool.connection() as conn:
            return conn.execute("DELETE FROM reviews WHERE user_id=%s", (user_id,)).rowcount
