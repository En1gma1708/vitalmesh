"""Behaviour every QaHistory must have; run against the in-memory twin and Postgres (D83)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vitalmesh_accounts import KEEP, QaHistory

T0 = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
ANSWER = {
    "text": "Aim for 2359 kcal.",
    "partial": False,
    "missing": [],
    "claims": [],
    "citations": [],
}


def check_question_then_answer(h: QaHistory) -> None:
    h.record_question("u-1", "c1", "How many calories?", T0)
    [e] = h.recent("u-1")
    assert (e.question, e.status, e.answer) == ("How many calories?", "asked", None)
    h.record_answer("u-1", "c1", "released", ANSWER, T0 + timedelta(seconds=3))
    [e] = h.recent("u-1")
    assert e.status == "released" and e.answer == ANSWER and e.question == "How many calories?"
    assert e.asked_at == T0


def check_redelivery_and_late_question_change_nothing(h: QaHistory) -> None:
    h.record_answer("u-1", "c1", "released", ANSWER, T0)  # the answer event arrives first
    h.record_question("u-1", "c1", "How many calories?", T0)  # then the question
    h.record_question("u-1", "c1", "How many calories?", T0)  # and a redelivery
    h.record_answer("u-1", "c1", "blocked", None, T0)  # a stray later status must not undo it
    [e] = h.recent("u-1")
    assert e.status == "released" and e.answer == ANSWER and e.question == "How many calories?"


def check_newest_first_and_limit(h: QaHistory) -> None:
    for i in range(5):
        h.record_question("u-1", f"c{i}", f"q{i}", T0 + timedelta(minutes=i))
    got = [e.question for e in h.recent("u-1", 3)]
    assert got == ["q4", "q3", "q2"]


def check_people_are_separate_and_purge_removes_only_theirs(h: QaHistory) -> None:
    h.record_question("u-1", "c1", "mine", T0)
    h.record_question("u-2", "c1", "theirs", T0)  # same correlation id, other person
    assert [e.question for e in h.recent("u-1")] == ["mine"]
    assert h.purge("u-1") == 1
    assert h.recent("u-1") == [] and [e.question for e in h.recent("u-2")] == ["theirs"]
    assert h.purge("u-1") == 0


def check_only_the_latest_are_kept(h: QaHistory) -> None:
    for i in range(KEEP + 5):
        h.record_question("u-1", f"c{i}", f"q{i}", T0 + timedelta(minutes=i))
    got = h.recent("u-1", KEEP + 10)
    assert len(got) == KEEP
    assert got[0].question == f"q{KEEP + 4}" and got[-1].question == "q5"


def check_unknown_status_is_refused(h: QaHistory) -> None:
    import pytest

    with pytest.raises(ValueError):
        h.record_answer("u-1", "c1", "whatever", None, T0)


CHECKS = [
    check_question_then_answer,
    check_redelivery_and_late_question_change_nothing,
    check_newest_first_and_limit,
    check_people_are_separate_and_purge_removes_only_theirs,
    check_only_the_latest_are_kept,
    check_unknown_status_is_refused,
]
