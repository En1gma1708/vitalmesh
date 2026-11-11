"""The "How you're doing" scorecard (D108): four areas, each with a status in plain words.

It uses the same calculators and thresholds as the progress helper
(``vitalmesh_wellness.bodystatus``), so the page and a typed answer can never disagree. A status
is words as well as a colour (on track, needs attention, not enough data), and every area says how
much data it rests on. No guessing: with too little data an area says what to log. Pure: the route
supplies the stores and the clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel
from vitalmesh_stores import HistoryStore, Point
from vitalmesh_wellness import bodystatus as bs
from vitalmesh_wellness.engine import Profile

LABELS = {
    bs.Status.ON_TRACK: "On track",
    bs.Status.NEEDS_ATTENTION: "Needs attention",
    bs.Status.NOT_ENOUGH_DATA: "Not enough data",
}
NO_VALUE = "\u2013"


class Area(BaseModel):
    key: str
    title: str
    status: str  # on_track | needs_attention | not_enough_data
    label: str
    headline: str
    detail: str


class StatusOut(BaseModel):
    areas: list[Area]
    caveat: str


CAVEAT = (
    "BMI is a rough guide: it ignores muscle, age and pregnancy. This is general wellness "
    "information, not a diagnosis."
)


def _area(key: str, title: str, status: bs.Status, headline: str, detail: str) -> Area:
    return Area(
        key=key,
        title=title,
        status=status.value,
        label=LABELS[status],
        headline=headline,
        detail=detail,
    )


def _points(
    history: HistoryStore | None, user_id: str, metric: str, since: datetime
) -> list[Point]:
    if history is None:
        return []
    try:
        return history.series(user_id, metric, since)
    except Exception:  # a history outage shows "not enough data", never an error page
        return []


def build_status(
    profile: Profile, history: HistoryStore | None, now: datetime, energy_target: float | None
) -> StatusOut:
    uid = profile.user_id
    weights = [
        (p.t, p.value) for p in _points(history, uid, "body_weight", now - timedelta(days=90))
    ]
    nights = {
        p.t.date(): p.value
        for p in _points(history, uid, "sleep_duration", now - timedelta(days=30))
    }
    active = _points(history, uid, "active_minutes", now - timedelta(days=7))
    energy = _points(history, uid, "energy_intake", now - timedelta(days=7))

    # sleep
    s = bs.sleep_summary(list(nights.items()), now.date())
    sleep_status = bs.sleep_status(s)
    if s.avg_7d is not None:
        sleep_area = _area(
            "sleep",
            "Sleep",
            sleep_status,
            f"{s.avg_7d:.1f} h",
            f"average over {s.nights_7d} nights; adults need at least {bs.SLEEP_MIN_HOURS:g}",
        )
    else:
        sleep_area = _area(
            "sleep",
            "Sleep",
            sleep_status,
            NO_VALUE,
            f"log {bs.SLEEP_MIN_NIGHTS} nights to see how it is going",
        )

    # weight
    trend = bs.weight_trend(weights, now)
    weight = trend.last_kg if trend.last_kg is not None else profile.weight_kg
    advice = bs.target_advice(weight, profile.height_cm, profile.goal.value, profile.age_years)
    w_status = bs.weight_status(advice, trend, profile.goal.value)
    detail = f"BMI {advice.bmi:.1f}, {advice.category.value} range"
    if trend.enough_data and trend.kg_per_week is not None and abs(trend.kg_per_week) >= 0.05:
        detail += (
            f"; {'down' if trend.kg_per_week < 0 else 'up'} {abs(trend.kg_per_week):.1f} kg a week"
        )
    elif not trend.enough_data:
        detail += "; log your weight over a week or more for a trend"
    if trend.stale and trend.days_since_last is not None:
        detail += f"; last reading {round(trend.days_since_last)} days ago"
    weight_area = _area("weight", "Weight", w_status, f"{weight:.1f} kg", detail)

    # activity
    minutes = sum(p.value for p in active) if active else None
    a_status = bs.activity_status(minutes)
    activity_area = _area(
        "activity",
        "Activity",
        a_status,
        f"{minutes:.0f} min" if minutes is not None else NO_VALUE,
        f"last 7 days; the weekly goal is {bs.WEEKLY_ACTIVE_MINUTES_GOAL:.0f}"
        if minutes is not None
        else "log some activity to see how it is going",
    )

    # intake
    by_day: dict[object, float] = {}
    for p in energy:
        by_day[p.t.date()] = by_day.get(p.t.date(), 0.0) + p.value
    avg = sum(by_day.values()) / len(by_day) if by_day else None
    i_status = bs.intake_status(avg, energy_target, len(by_day))
    if avg is not None and energy_target and len(by_day) >= bs.INTAKE_MIN_DAYS:
        intake_area = _area(
            "intake",
            "Food",
            i_status,
            f"{avg:,.0f} kcal",
            f"daily average over {len(by_day)} days; your target is {energy_target:,.0f}",
        )
    else:
        intake_area = _area(
            "intake",
            "Food",
            i_status,
            NO_VALUE,
            f"log meals for {bs.INTAKE_MIN_DAYS} days to compare with your target",
        )
    return StatusOut(areas=[sleep_area, weight_area, activity_area, intake_area], caveat=CAVEAT)
