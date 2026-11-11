"""Body and progress calculators for everyday questions (D108): pure code, no model, no I/O.

Every number the "how am I doing", "how is my weight" and "what should my target weight be"
answers state comes from here, and every threshold is a named constant below (never a prompt).

* BMI categories are the WHO adult bands (underweight under 18.5, healthy 18.5 to 24.9,
  overweight 25 to 29.9, obesity 30 and over). BMI is a crude screen: it ignores muscle, age,
  ethnicity and pregnancy, so the healthy weight is always a RANGE and carries a caveat.
* A weight-loss milestone is 5 percent of the current weight at 0.25 to 0.5 kg a week, and only
  for an adult under 65 with a BMI in the overweight band or above who chose the goal "lose".
  Underweight, in-range, older adults and the goal "gain" get NO loss target.
* A trend needs at least 3 readings spanning at least 7 days; otherwise it says so. Nothing is
  extrapolated past the data, and a last reading older than 14 days is marked stale.
* Sleep: adults need "at least 7-8 hours" a night (NHLBI, Your Guide to Healthy Sleep, 2011,
  ingested into the corpus so the answer can cite it). The line used here is 7 hours: a week
  averaging under 7 needs attention; there is no upper limit because no ingested source gives
  one. A trend needs 3 or more nights.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

BMI_UNDERWEIGHT = 18.5
BMI_HEALTHY_HIGH = 24.9
BMI_OVERWEIGHT_FROM = 25.0
BMI_OBESITY_FROM = 30.0

MILESTONE_FRACTION = 0.05  # first loss milestone: 5 percent of the current weight
MILESTONE_RATE_KG_PER_WEEK = (0.25, 0.5)
OLDER_ADULT_AGE = 65.0

TREND_MIN_POINTS = 3
TREND_MIN_SPAN_DAYS = 7.0
TREND_WINDOW_DAYS = 30
STALE_AFTER_DAYS = 14.0
SAFE_LOSS_FRACTION_PER_WEEK = 0.01

SLEEP_MIN_HOURS = 7.0  # the low end of the cited 7-8 hours for adults
SLEEP_MIN_NIGHTS = 3
WEEKLY_ACTIVE_MINUTES_GOAL = 150.0


class BodyError(ValueError):
    """An input outside what the calculators accept."""


class Status(StrEnum):
    ON_TRACK = "on_track"
    NEEDS_ATTENTION = "needs_attention"
    NOT_ENOUGH_DATA = "not_enough_data"


class BmiCategory(StrEnum):
    UNDERWEIGHT = "underweight"
    HEALTHY = "healthy"
    OVERWEIGHT = "overweight"
    OBESITY = "obesity"


def _positive(name: str, value: float, low: float, high: float) -> float:
    if not math.isfinite(value) or not low <= value <= high:
        raise BodyError(f"{name} must be between {low:g} and {high:g}, got {value!r}")
    return value


def bmi(weight_kg: float, height_cm: float) -> float:
    w = _positive("weight_kg", weight_kg, 20.0, 400.0)
    h = _positive("height_cm", height_cm, 100.0, 250.0)
    return w / (h / 100.0) ** 2


def bmi_category(value: float) -> BmiCategory:
    if value < BMI_UNDERWEIGHT:
        return BmiCategory.UNDERWEIGHT
    if value < BMI_OVERWEIGHT_FROM:
        return BmiCategory.HEALTHY
    if value < BMI_OBESITY_FROM:
        return BmiCategory.OVERWEIGHT
    return BmiCategory.OBESITY


@dataclass(frozen=True)
class WeightRange:
    low_kg: float
    high_kg: float


def healthy_weight_range(height_cm: float) -> WeightRange:
    """Weights whose BMI is 18.5 to 24.9 for this height, to one decimal place.

    The ends are rounded INWARDS, so a weight at either end is always inside the BMI band.
    """
    h = _positive("height_cm", height_cm, 100.0, 250.0) / 100.0
    low = math.ceil(BMI_UNDERWEIGHT * h * h * 10) / 10
    high = math.floor(BMI_HEALTHY_HIGH * h * h * 10) / 10
    return WeightRange(low_kg=low, high_kg=high)


class TargetKind(StrEnum):
    IN_RANGE = "in_range"
    LOSE_GRADUALLY = "lose_gradually"
    ABOVE_RANGE_NO_GOAL = "above_range_no_goal"
    UNDERWEIGHT_NO_LOSS = "underweight_no_loss"
    GAIN_GOAL = "gain_goal"
    OLDER_ADULT = "older_adult"


@dataclass(frozen=True)
class TargetAdvice:
    kind: TargetKind
    bmi: float
    category: BmiCategory
    healthy: WeightRange
    milestone_kg: float | None = None  # a weight to reach first, never below the healthy range
    milestone_loss_kg: float | None = None
    weeks_min: int | None = None
    weeks_max: int | None = None


def target_advice(weight_kg: float, height_cm: float, goal: str, age_years: float) -> TargetAdvice:
    """What can be said about a target weight. Never one "ideal" number and never a loss target
    for someone underweight, in range, 65 or older, or whose goal is to gain."""
    b = bmi(weight_kg, height_cm)
    cat = bmi_category(b)
    rng = healthy_weight_range(height_cm)

    def plain(kind: TargetKind) -> TargetAdvice:
        return TargetAdvice(kind=kind, bmi=b, category=cat, healthy=rng)

    if age_years >= OLDER_ADULT_AGE:
        return plain(TargetKind.OLDER_ADULT)
    if cat is BmiCategory.UNDERWEIGHT:
        return plain(TargetKind.UNDERWEIGHT_NO_LOSS)
    if goal == "gain":
        return plain(TargetKind.GAIN_GOAL)
    if cat is BmiCategory.HEALTHY:
        return plain(TargetKind.IN_RANGE)
    if goal != "lose":
        return plain(TargetKind.ABOVE_RANGE_NO_GOAL)
    loss = round(weight_kg * MILESTONE_FRACTION, 1)
    milestone = max(round(weight_kg - loss, 1), rng.high_kg)
    loss = round(weight_kg - milestone, 1)
    fast, slow = MILESTONE_RATE_KG_PER_WEEK[1], MILESTONE_RATE_KG_PER_WEEK[0]
    return TargetAdvice(
        kind=TargetKind.LOSE_GRADUALLY,
        bmi=b,
        category=cat,
        healthy=rng,
        milestone_kg=milestone,
        milestone_loss_kg=loss,
        weeks_min=math.ceil(loss / fast),
        weeks_max=math.ceil(loss / slow),
    )


@dataclass(frozen=True)
class WeightTrend:
    n_points: int
    last_kg: float | None
    days_since_last: float | None
    stale: bool
    change_7d_kg: float | None
    change_30d_kg: float | None
    change_90d_kg: float | None
    kg_per_week: float | None  # least-squares slope over the last 30 days; None if too thin
    enough_data: bool


def _change_over(points: Sequence[tuple[datetime, float]], days: int) -> float | None:
    """Last reading minus the earliest reading inside the window, if the window is covered by at
    least half its length; otherwise None (no guess)."""
    last_t, last_v = points[-1]
    inside = [(t, v) for t, v in points if t >= last_t - timedelta(days=days)]
    if len(inside) < 2:
        return None
    span = (inside[-1][0] - inside[0][0]).total_seconds() / 86400.0
    if span < days / 2:
        return None
    return round(last_v - inside[0][1], 2)


def weight_trend(points: Sequence[tuple[datetime, float]], now: datetime) -> WeightTrend:
    """Summarise body-weight readings (kg). Input in any order; at most one reading per instant."""
    pts = sorted(points)
    if any(not math.isfinite(v) or v <= 0 for _, v in pts):
        raise BodyError("weights must be finite and positive")
    if not pts:
        return WeightTrend(0, None, None, False, None, None, None, None, False)
    last_t, last_v = pts[-1]
    age = max((now - last_t).total_seconds() / 86400.0, 0.0)
    recent = [(t, v) for t, v in pts if t >= last_t - timedelta(days=TREND_WINDOW_DAYS)]
    span = (recent[-1][0] - recent[0][0]).total_seconds() / 86400.0
    enough = len(recent) >= TREND_MIN_POINTS and span >= TREND_MIN_SPAN_DAYS
    slope: float | None = None
    if enough:
        xs = [(t - recent[0][0]).total_seconds() / 86400.0 for t, _ in recent]
        ys = [v for _, v in recent]
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        slope = round(sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx * 7, 2)
    return WeightTrend(
        n_points=len(pts),
        last_kg=last_v,
        days_since_last=round(age, 1),
        stale=age > STALE_AFTER_DAYS,
        change_7d_kg=_change_over(pts, 7),
        change_30d_kg=_change_over(pts, 30),
        change_90d_kg=_change_over(pts, 90),
        kg_per_week=slope,
        enough_data=enough,
    )


@dataclass(frozen=True)
class SleepSummary:
    nights_logged: int
    nights_7d: int
    avg_7d: float | None
    avg_30d: float | None
    nights_below_min_7d: int
    spread_7d: float | None  # population standard deviation of the last 7 nights
    change_vs_previous_week: float | None  # average of the last 7 days minus the 7 before
    enough_data: bool


def sleep_summary(nights: Sequence[tuple[date, float]], today: date) -> SleepSummary:
    """Summarise nightly sleep durations (hours) ending at `today`. Durations only: nothing here
    says anything about bedtimes."""
    by_day: dict[date, float] = {}
    for d, h in nights:
        if not math.isfinite(h) or not 0.0 <= h <= 24.0:
            raise BodyError("sleep hours must be between 0 and 24")
        if d <= today:
            by_day[d] = h
    last7 = [h for d, h in by_day.items() if today - timedelta(days=7) < d <= today]
    prev7 = [h for d, h in by_day.items() if today - timedelta(days=14) < d <= today - timedelta(7)]
    last30 = [h for d, h in by_day.items() if today - timedelta(days=30) < d <= today]
    enough = len(last7) >= SLEEP_MIN_NIGHTS

    def mean(xs: list[float]) -> float | None:
        return round(statistics.fmean(xs), 1) if xs else None

    change = None
    if enough and len(prev7) >= SLEEP_MIN_NIGHTS:
        change = round(statistics.fmean(last7) - statistics.fmean(prev7), 1)
    return SleepSummary(
        nights_logged=len(by_day),
        nights_7d=len(last7),
        avg_7d=mean(last7) if enough else None,
        avg_30d=mean(last30) if len(last30) >= SLEEP_MIN_NIGHTS else None,
        nights_below_min_7d=sum(1 for h in last7 if h < SLEEP_MIN_HOURS),
        spread_7d=round(statistics.pstdev(last7), 1) if enough else None,
        change_vs_previous_week=change,
        enough_data=enough,
    )


def sleep_status(s: SleepSummary) -> Status:
    if not s.enough_data or s.avg_7d is None:
        return Status.NOT_ENOUGH_DATA
    return Status.ON_TRACK if s.avg_7d >= SLEEP_MIN_HOURS else Status.NEEDS_ATTENTION


def activity_status(active_minutes_last_7d: float | None) -> Status:
    if active_minutes_last_7d is None:
        return Status.NOT_ENOUGH_DATA
    return (
        Status.ON_TRACK
        if active_minutes_last_7d >= WEEKLY_ACTIVE_MINUTES_GOAL
        else Status.NEEDS_ATTENTION
    )


def weight_status(advice: TargetAdvice, trend: WeightTrend, goal: str) -> Status:
    """Healthy BMI and not changing unsafely is on track; above the band with the goal to lose is
    on track only while the trend is downwards; below the band always needs attention."""
    if (
        trend.enough_data
        and trend.kg_per_week is not None
        and trend.last_kg is not None
        and trend.kg_per_week < -SAFE_LOSS_FRACTION_PER_WEEK * trend.last_kg
    ):
        return Status.NEEDS_ATTENTION  # losing faster than 1 percent of body weight a week
    cat = advice.category
    if cat is BmiCategory.UNDERWEIGHT:
        return Status.NEEDS_ATTENTION
    if cat is BmiCategory.HEALTHY:
        return Status.ON_TRACK
    if goal == "lose":
        if not trend.enough_data:
            return Status.NOT_ENOUGH_DATA
        return (
            Status.ON_TRACK
            if trend.kg_per_week is not None and trend.kg_per_week < 0
            else Status.NEEDS_ATTENTION
        )
    return Status.NEEDS_ATTENTION
