"""The Scheduler's rules (D80): pure code, so every rule is a plain test."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest
from vitalmesh_api.googlecal import event_id, pkce_pair
from vitalmesh_api.scheduler import (
    CONDITION_NOTICE,
    DAY_END,
    DAY_START,
    SESSION_MINUTES,
    propose,
    refusal_for,
    sessions_needed,
)

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 5, 10, 0, tzinfo=IST)  # a Thursday morning


def at(day_offset: int, hh: int, mm: int = 0) -> datetime:
    day = NOW.date() + timedelta(days=day_offset)
    return datetime.combine(day, time(hh, mm), tzinfo=IST)


def test_sessions_needed_reaches_the_weekly_goal_up_to_five() -> None:
    assert {m: sessions_needed(m) for m in SESSION_MINUTES} == {20: 5, 30: 5, 45: 4, 60: 3}


def test_slots_are_in_waking_hours_on_the_grid_and_start_tomorrow() -> None:
    plan = propose([], IST, NOW, 45)
    assert len(plan.slots) == 4 and plan.short_by == 0
    for s in plan.slots:
        assert s.start.date() > NOW.date()  # never today, never the past
        assert DAY_START <= s.start.timetz().replace(tzinfo=None) <= DAY_END
        assert s.end.timetz().replace(tzinfo=None) <= DAY_END
        assert s.start.minute in (0, 30) and (s.end - s.start) == timedelta(minutes=45)
        assert s.start.utcoffset() == timedelta(hours=5, minutes=30)


def test_the_slot_closest_to_six_in_the_evening_is_preferred() -> None:
    plan = propose([], IST, NOW, 30)
    assert all(s.start.time() == time(18, 0) for s in plan.slots)


def test_a_busy_evening_moves_the_slot_to_the_nearest_free_time() -> None:
    busy = [(at(1, 17, 0), at(1, 19, 0))]
    first = propose(busy, IST, NOW, 30).slots[0]
    assert first.start.date() == at(1, 0).date()
    # 15 minutes clear of the event on either side: 19:15 is too close to 19:00 -> 19:30 is the
    # first free grid time after, 16:30 the last before; 16:30 is nearer to 18:00 than 19:30
    assert first.start.time() in (time(16, 30), time(19, 30))
    assert abs((first.start - at(1, 18)).total_seconds()) <= 90 * 60


def test_no_slot_overlaps_or_touches_a_busy_event() -> None:
    busy = [(at(d, 9, 0), at(d, 17, 30)) for d in range(1, 8)]
    for s in propose(busy, IST, NOW, 60).slots:
        for b_start, b_end in busy:
            assert s.end + timedelta(minutes=15) <= b_start or s.start >= b_end + timedelta(
                minutes=15
            )


def test_sessions_are_spread_not_back_to_back() -> None:
    days = [s.start.date() for s in propose([], IST, NOW, 60).slots]
    assert len(days) == 3
    assert all((b - a).days >= 2 for a, b in pairwise(days))


def test_a_full_calendar_gives_no_slots_and_a_partial_one_says_how_many_are_missing() -> None:
    full = [(at(d, 0, 0), at(d, 23, 59)) for d in range(1, 9)]
    assert propose(full, IST, NOW, 30).slots == []
    only_two = [(at(d, 0, 0), at(d, 23, 59)) for d in (1, 2, 4, 5, 7)]  # days 3 and 6 stay free
    plan = propose(only_two, IST, NOW, 30)
    assert len(plan.slots) == 2 and plan.short_by == 3 and plan.total_minutes == 60


def test_the_same_inputs_always_give_the_same_proposal() -> None:
    busy = [(at(2, 12, 0), at(2, 13, 0))]
    assert propose(busy, IST, NOW, 30) == propose(busy, IST, NOW, 30)


def test_a_session_length_outside_the_closed_list_is_refused() -> None:
    with pytest.raises(ValueError):
        propose([], IST, NOW, 25)


def test_a_declared_condition_gets_a_notice_instead_of_a_schedule() -> None:
    assert refusal_for([]) is None
    assert refusal_for(["hypertension"]) == CONDITION_NOTICE


def test_event_ids_are_stable_valid_and_person_specific() -> None:
    start = at(1, 18).astimezone(UTC)
    a = event_id("u-1", start)
    assert a == event_id("u-1", start) and a != event_id("u-2", start)
    assert 5 <= len(a) <= 1024 and set(a) <= set("0123456789abcdefghijklmnopqrstuv")


def test_pkce_challenge_is_the_s256_of_the_verifier() -> None:
    import base64
    import hashlib

    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).decode().rstrip("=")
    assert pkce_pair()[0] != verifier
