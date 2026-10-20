"""Chart arithmetic for the health app (D53): trailing average and weekly totals. Pure.

Time-based, not count-based: a user who logs weight every other day and one who logs daily get
the same 7-day window, so the line means the same thing for both.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime, timedelta

WEEKLY_ACTIVE_MINUTES_GOAL = 150.0  # common public guidance for adults; shown as a goal line


def trailing_average(
    points: Sequence[tuple[datetime, float]], window_days: int = 7
) -> list[tuple[datetime, float]]:
    """Mean of the points in the window ending at each point (inclusive). Input sorted by time."""
    if window_days < 1:
        raise ValueError("window must be at least one day")
    if any(not math.isfinite(v) for _, v in points):
        raise ValueError("series contains a non-finite value")
    window = timedelta(days=window_days)
    out: list[tuple[datetime, float]] = []
    start = 0
    running = 0.0
    for i, (t, v) in enumerate(points):
        running += v
        while points[start][0] <= t - window:
            running -= points[start][1]
            start += 1
        out.append((t, running / (i - start + 1)))
    return out


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def weekly_totals(points: Sequence[tuple[datetime, float]]) -> list[tuple[date, float]]:
    """Sum per ISO week (Monday start), oldest first. Weeks without data are omitted."""
    totals: dict[date, float] = {}
    for t, v in points:
        week = monday_of(t.date())
        totals[week] = totals.get(week, 0.0) + v
    return sorted(totals.items())
