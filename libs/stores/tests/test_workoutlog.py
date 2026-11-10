"""The suggested-workout log (D104): in memory here; the Redis twin is covered with the others in
test_person_index (integration)."""

from __future__ import annotations

from datetime import date, timedelta

from vitalmesh_stores import InMemoryWorkoutLog
from vitalmesh_stores.workoutlog import KEEP_DAYS

DAY = date(2026, 11, 12)


def test_a_recorded_session_is_read_back_for_that_person_only() -> None:
    log = InMemoryWorkoutLog()
    log.record("a", DAY, "push")
    assert log.recent("a") == {DAY: "push"}
    assert log.recent("b") == {}


def test_recording_the_same_day_again_replaces_it() -> None:
    log = InMemoryWorkoutLog()
    log.record("a", DAY, "push")
    log.record("a", DAY, "pull")
    assert log.recent("a") == {DAY: "pull"}


def test_only_the_last_fourteen_days_are_kept() -> None:
    log = InMemoryWorkoutLog()
    for i in range(KEEP_DAYS + 5):
        log.record("a", DAY + timedelta(days=i), "push")
    kept = log.recent("a")
    assert len(kept) == KEEP_DAYS
    assert min(kept) == DAY + timedelta(days=5)
    assert max(kept) == DAY + timedelta(days=KEEP_DAYS + 4)


def test_the_returned_dict_is_a_copy() -> None:
    log = InMemoryWorkoutLog()
    log.record("a", DAY, "push")
    log.recent("a").clear()
    assert log.recent("a") == {DAY: "push"}
