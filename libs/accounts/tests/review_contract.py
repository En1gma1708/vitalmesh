"""Behaviour every ReviewQueue must have; run against the in-memory twin and Postgres (D85)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from vitalmesh_accounts import APPROVED, PENDING, REJECTED, ReviewCase, ReviewQueue, review_export

T0 = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)


def case(rec: str = "rec-1", minutes: int = 0, question: str = "what should I eat?") -> ReviewCase:
    return ReviewCase(
        recommendation_id=rec,
        correlation_id=f"corr-{rec}",
        requested_at=T0 + timedelta(minutes=minutes),
        question=question,
        briefing="A marked lab value needs a look.",
        reasons=["risk.high"],
        draft={"text": "Eat more fibre.", "risk": "high", "claims": [], "citations": []},
    )


def check_request_then_decision(q: ReviewQueue) -> None:
    q.put_request("u-1", case())
    got = q.get("rec-1")
    assert got is not None and got.status == PENDING and got.question == "what should I eat?"
    assert got.reasons == ["risk.high"] and got.draft["text"] == "Eat more fibre."
    assert q.record_decision("rec-1", APPROVED, "clin-1", T0 + timedelta(minutes=5), "fine") is True
    done = q.get("rec-1")
    assert done is not None and done.status == APPROVED and done.decided_by == "clin-1"
    assert done.note == "fine" and done.decided_at == T0 + timedelta(minutes=5)


def check_a_decided_case_never_changes_again(q: ReviewQueue) -> None:
    q.put_request("u-1", case())
    assert q.record_decision("rec-1", APPROVED, "clin-1", T0, "first") is True
    assert q.record_decision("rec-1", REJECTED, "clin-2", T0, "second") is False
    again = q.get("rec-1")
    assert again is not None and again.status == APPROVED and again.decided_by == "clin-1"
    q.put_request("u-1", case())  # a redelivered request must not reopen it
    reopened = q.get("rec-1")
    assert reopened is not None and reopened.status == APPROVED


def check_a_decision_that_arrives_first_is_kept_and_the_detail_added_later(q: ReviewQueue) -> None:
    assert q.record_decision("rec-1", REJECTED, "clin-1", T0, "no", user_id="u-1") is True
    assert q.cases(PENDING) == []  # a stub without detail is not shown
    q.put_request("u-1", case())
    got = q.get("rec-1")
    assert got is not None and got.status == REJECTED and got.question == "what should I eat?"
    assert [c.recommendation_id for c in q.cases(REJECTED)] == ["rec-1"]


def check_pending_oldest_first_and_decided_newest_first(q: ReviewQueue) -> None:
    for i, rec in enumerate(["a", "b", "c"]):
        q.put_request("u-1", case(rec, minutes=i))
    assert [c.recommendation_id for c in q.cases(PENDING)] == ["a", "b", "c"]
    q.record_decision("a", APPROVED, "clin", T0 + timedelta(minutes=10), "")
    q.record_decision("b", REJECTED, "clin", T0 + timedelta(minutes=20), "")
    assert [c.recommendation_id for c in q.cases(PENDING)] == ["c"]
    assert [c.recommendation_id for c in q.cases("decided")] == ["b", "a"]
    assert len(q.cases("decided", limit=1)) == 1


def check_owner_export_and_purge(q: ReviewQueue) -> None:
    q.put_request("u-1", case("mine"))
    q.put_request("u-2", case("theirs", question="their private question"))
    assert q.owner("mine") == "u-1" and q.owner("nope") is None
    exported = review_export(q, "u-1")
    assert [e["question"] for e in exported] == ["what should I eat?"]
    assert "their private question" not in str(exported)
    assert q.purge("u-1") == 1
    assert q.get("mine") is None and q.get("theirs") is not None
    assert q.purge("u-1") == 0


def check_unknown_decision_is_refused(q: ReviewQueue) -> None:
    with pytest.raises(ValueError):
        q.record_decision("rec-1", "maybe", "clin-1", T0, "")


CHECKS = [
    check_request_then_decision,
    check_a_decided_case_never_changes_again,
    check_a_decision_that_arrives_first_is_kept_and_the_detail_added_later,
    check_pending_oldest_first_and_decided_newest_first,
    check_owner_export_and_purge,
    check_unknown_decision_is_refused,
]
