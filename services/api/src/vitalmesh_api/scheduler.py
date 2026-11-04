"""Propose workout times around a person's calendar (D80).

Pure code: the same busy times and settings always give the same proposal, and no language model
is involved (times and amounts are numbers; the model never decides them). The proposal is only a
suggestion: nothing is written to the calendar until the person confirms it.

Rules, each with a test:
* sessions are 20, 30, 45 or 60 minutes (a closed list); the number per week is what reaches the
  weekly active-minutes goal, at most 5, never more than one a day;
* only between 07:00 and 20:00 local time, on a half-hour grid, starting tomorrow (never in the
  past), within the next 7 days;
* never overlapping an event, and at least 15 minutes clear of one on each side;
* within a free day the slot closest to 18:00 is chosen; days are spread out (1st, 3rd, 5th... day
  first) so sessions do not all fall back to back;
* a person who declared a health condition is not given a schedule (an exercise plan for a
  medical condition is a clinician's decision): they are told to ask first.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from vitalmesh_wellness.trends import WEEKLY_ACTIVE_MINUTES_GOAL

SESSION_MINUTES = (20, 30, 45, 60)
MAX_SESSIONS = 5
DAY_START = time(7, 0)
DAY_END = time(20, 0)
PREFERRED = time(18, 0)
BUFFER = timedelta(minutes=15)
GRID_MINUTES = 30
HORIZON_DAYS = 7
DAY_ORDER = (0, 2, 4, 6, 1, 3, 5)  # offsets from tomorrow: spread the week out

CONDITION_NOTICE = (
    "You listed a health condition, so please ask your clinician what activity is right for you "
    "before scheduling workouts."
)


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Proposal:
    minutes: int
    wanted: int
    slots: list[Slot] = field(default_factory=list)

    @property
    def short_by(self) -> int:
        return max(0, self.wanted - len(self.slots))

    @property
    def total_minutes(self) -> int:
        return self.minutes * len(self.slots)


def refusal_for(conditions: list[str]) -> str | None:
    return CONDITION_NOTICE if conditions else None


def sessions_needed(minutes: int) -> int:
    return min(MAX_SESSIONS, math.ceil(WEEKLY_ACTIVE_MINUTES_GOAL / minutes))


def _day_candidates(
    day_start: datetime, minutes: int, busy: list[tuple[datetime, datetime]]
) -> list[datetime]:
    zone = day_start.tzinfo
    first = datetime.combine(day_start.date(), DAY_START, tzinfo=zone)
    last_end = datetime.combine(day_start.date(), DAY_END, tzinfo=zone)
    out: list[datetime] = []
    start = first
    while start + timedelta(minutes=minutes) <= last_end:
        end = start + timedelta(minutes=minutes)
        padded = (start - BUFFER, end + BUFFER)
        if not any(padded[0] < b_end and b_start < padded[1] for b_start, b_end in busy):
            out.append(start)
        start += timedelta(minutes=GRID_MINUTES)
    return out


def propose(
    busy: list[tuple[datetime, datetime]], tz: ZoneInfo, now: datetime, minutes: int
) -> Proposal:
    if minutes not in SESSION_MINUTES:
        raise ValueError(f"minutes must be one of {SESSION_MINUTES}")
    wanted = sessions_needed(minutes)
    tomorrow = now.astimezone(tz).date() + timedelta(days=1)
    chosen: list[Slot] = []
    for offset in DAY_ORDER[:HORIZON_DAYS]:
        if len(chosen) == wanted:
            break
        day = datetime.combine(tomorrow + timedelta(days=offset), time(0, 0), tzinfo=tz)
        candidates = _day_candidates(day, minutes, busy)
        if not candidates:
            continue
        target = datetime.combine(day.date(), PREFERRED, tzinfo=tz)
        best = min(candidates, key=lambda c: (abs((c - target).total_seconds()), c))
        chosen.append(Slot(best, best + timedelta(minutes=minutes)))
    chosen.sort(key=lambda s: s.start)
    return Proposal(minutes, wanted, chosen)
