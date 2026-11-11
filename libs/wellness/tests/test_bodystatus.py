"""Calculators for everyday questions (D108): known values, boundaries and properties."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st
from vitalmesh_wellness.bodystatus import (
    BMI_HEALTHY_HIGH,
    BMI_UNDERWEIGHT,
    BmiCategory,
    BodyError,
    Status,
    TargetKind,
    activity_status,
    bmi,
    bmi_category,
    healthy_weight_range,
    sleep_status,
    sleep_summary,
    target_advice,
    weight_status,
    weight_trend,
)

NOW = datetime(2026, 11, 11, 12, 0)
TODAY = date(2026, 11, 11)


def series(
    values: list[float], every_days: float = 1.0, end_days_ago: float = 0.0
) -> list[tuple[datetime, float]]:
    n = len(values)
    return [
        (NOW - timedelta(days=end_days_ago + (n - 1 - i) * every_days), v)
        for i, v in enumerate(values)
    ]


def test_bmi_known_value_and_bands() -> None:
    assert bmi(70, 175) == pytest.approx(22.857, abs=0.001)
    assert bmi_category(18.49) is BmiCategory.UNDERWEIGHT
    assert bmi_category(18.5) is BmiCategory.HEALTHY
    assert bmi_category(24.99) is BmiCategory.HEALTHY
    assert bmi_category(25.0) is BmiCategory.OVERWEIGHT
    assert bmi_category(30.0) is BmiCategory.OBESITY


@pytest.mark.parametrize(
    "w,h", [(0, 170), (19, 170), (401, 170), (70, 99), (70, 251), (float("nan"), 170)]
)
def test_bmi_rejects_impossible_inputs(w: float, h: float) -> None:
    with pytest.raises(BodyError):
        bmi(w, h)


def test_healthy_range_known_height() -> None:
    r = healthy_weight_range(175)
    assert (r.low_kg, r.high_kg) == (56.7, 76.2)


@given(st.floats(min_value=120, max_value=230))
def test_range_ends_are_inside_the_bmi_band(height: float) -> None:
    r = healthy_weight_range(height)
    assert bmi(r.low_kg, height) >= BMI_UNDERWEIGHT - 1e-9
    assert bmi(r.high_kg, height) <= BMI_HEALTHY_HIGH + 1e-9
    assert r.low_kg < r.high_kg


@given(st.floats(min_value=120, max_value=230))
def test_a_taller_person_has_a_higher_range(height: float) -> None:
    assert healthy_weight_range(height + 1).high_kg > healthy_weight_range(height).high_kg


@given(
    weight=st.floats(min_value=30, max_value=250),
    height=st.floats(min_value=140, max_value=210),
    goal=st.sampled_from(["lose", "maintain", "gain"]),
    age=st.floats(min_value=18, max_value=95),
)
def test_a_target_is_never_below_the_healthy_range_and_never_for_the_excluded(
    weight: float, height: float, goal: str, age: float
) -> None:
    a = target_advice(weight, height, goal, age)
    if a.milestone_kg is not None:
        assert a.kind is TargetKind.LOSE_GRADUALLY
        assert a.milestone_kg >= a.healthy.high_kg - 1e-9
        assert a.milestone_kg < weight
        assert a.category in (BmiCategory.OVERWEIGHT, BmiCategory.OBESITY)
        assert goal == "lose" and age < 65
        assert a.weeks_min is not None and a.weeks_max is not None
        assert 1 <= a.weeks_min <= a.weeks_max
    if a.category is BmiCategory.UNDERWEIGHT and age < 65:
        assert a.kind is TargetKind.UNDERWEIGHT_NO_LOSS and a.milestone_kg is None
    if age >= 65 or goal == "gain":
        assert a.milestone_kg is None


def test_loss_milestone_example() -> None:
    a = target_advice(95, 175, "lose", 40)  # BMI 31.0
    assert a.kind is TargetKind.LOSE_GRADUALLY and a.category is BmiCategory.OBESITY
    assert a.milestone_kg == 90.2 and a.milestone_loss_kg == 4.8
    assert (a.weeks_min, a.weeks_max) == (10, 20)


def test_milestone_is_capped_at_the_top_of_the_healthy_range() -> None:
    a = target_advice(78, 175, "lose", 30)  # BMI 25.5: 5 percent would be 74.1, range tops at 76.2
    assert a.milestone_kg == 76.2 and a.milestone_loss_kg == 1.8


def test_in_range_and_other_kinds() -> None:
    assert target_advice(70, 175, "lose", 30).kind is TargetKind.IN_RANGE
    assert target_advice(95, 175, "maintain", 30).kind is TargetKind.ABOVE_RANGE_NO_GOAL
    assert target_advice(95, 175, "gain", 30).kind is TargetKind.GAIN_GOAL
    assert target_advice(95, 175, "lose", 70).kind is TargetKind.OLDER_ADULT
    assert target_advice(50, 175, "lose", 30).kind is TargetKind.UNDERWEIGHT_NO_LOSS


def test_trend_needs_three_points_over_a_week() -> None:
    t = weight_trend(series([80, 79.5]), NOW)
    assert not t.enough_data and t.kg_per_week is None and t.n_points == 2
    t = weight_trend(series([80, 79.8, 79.6], every_days=1), NOW)  # spans 2 days only
    assert not t.enough_data and t.kg_per_week is None


def test_trend_slope_and_changes_on_a_known_series() -> None:
    # 0.1 kg a day downwards for 15 days: 0.7 kg a week
    values = [90 - 0.1 * i for i in range(15)]
    t = weight_trend(series(values), NOW)
    assert t.enough_data and t.kg_per_week == pytest.approx(-0.7, abs=0.01)
    assert t.change_7d_kg == pytest.approx(-0.7, abs=0.01)
    # 14 days of data cover less than half of a 30 or 90 day window: no change is claimed
    assert t.change_30d_kg is None and t.change_90d_kg is None
    assert t.last_kg == pytest.approx(88.6) and not t.stale and t.days_since_last == 0.0


def test_a_change_needs_the_window_to_be_half_covered() -> None:
    t = weight_trend(series([80, 80.2, 80.1, 80.0], every_days=1), NOW)  # 3 days of data
    assert t.change_7d_kg is None and t.change_30d_kg is None


def test_staleness_and_empty() -> None:
    t = weight_trend(series([80, 80, 80, 80], every_days=3, end_days_ago=23), NOW)
    assert t.stale and t.days_since_last == 23.0
    e = weight_trend([], NOW)
    assert e.n_points == 0 and e.last_kg is None and not e.enough_data


def test_trend_rejects_bad_values() -> None:
    with pytest.raises(BodyError):
        weight_trend([(NOW, -1.0)], NOW)


@given(
    values=st.lists(st.floats(min_value=40, max_value=150), min_size=3, max_size=40),
    shift=st.floats(min_value=-20, max_value=20),
)
def test_the_slope_ignores_a_constant_shift(values: list[float], shift: float) -> None:
    a = weight_trend(series(values), NOW)
    b = weight_trend(series([v + shift + 21 for v in values]), NOW)
    if a.kg_per_week is not None and b.kg_per_week is not None:
        assert a.kg_per_week == pytest.approx(b.kg_per_week, abs=0.02)


@given(st.lists(st.floats(min_value=40, max_value=150), min_size=8, max_size=40))
def test_order_of_input_does_not_matter(values: list[float]) -> None:
    pts = series(values)
    assert weight_trend(pts, NOW) == weight_trend(list(reversed(pts)), NOW)


def nights(values: list[float], end_days_ago: int = 0) -> list[tuple[date, float]]:
    n = len(values)
    return [(TODAY - timedelta(days=end_days_ago + n - 1 - i), v) for i, v in enumerate(values)]


def test_sleep_summary_known_week() -> None:
    s = sleep_summary(nights([8, 7.5, 6, 7, 9.5, 8, 7]), TODAY)
    assert s.nights_7d == 7 and s.avg_7d == 7.6 and s.nights_below_min_7d == 1
    assert s.enough_data
    assert sleep_status(s) is Status.ON_TRACK


def test_sleep_needs_attention_when_the_average_is_short() -> None:
    s = sleep_summary(nights([6, 5.5, 6.5, 6, 6, 7, 5]), TODAY)
    assert s.avg_7d == 6.0 and sleep_status(s) is Status.NEEDS_ATTENTION


def test_sleep_with_too_few_nights_says_so() -> None:
    s = sleep_summary(nights([8, 8]), TODAY)
    assert not s.enough_data and s.avg_7d is None and sleep_status(s) is Status.NOT_ENOUGH_DATA


def test_sleep_this_week_against_last() -> None:
    s = sleep_summary(nights([6, 6, 6, 6, 6, 6, 6, 7, 7, 7, 7, 7, 7, 7]), TODAY)
    assert s.change_vs_previous_week == 1.0
    assert sleep_summary(nights([7, 7, 7]), TODAY).change_vs_previous_week is None


def test_sleep_ignores_the_future_and_rejects_nonsense() -> None:
    s = sleep_summary([(TODAY + timedelta(days=1), 3.0), *nights([7, 7, 7])], TODAY)
    assert s.nights_logged == 3
    with pytest.raises(BodyError):
        sleep_summary([(TODAY, 30.0)], TODAY)


@given(st.lists(st.floats(min_value=0, max_value=14), min_size=3, max_size=7))
def test_the_average_lies_between_the_extremes(values: list[float]) -> None:
    s = sleep_summary(nights(values), TODAY)
    assert s.avg_7d is not None
    assert min(values) - 0.06 <= s.avg_7d <= max(values) + 0.06


def test_activity_status() -> None:
    assert activity_status(None) is Status.NOT_ENOUGH_DATA
    assert activity_status(149.9) is Status.NEEDS_ATTENTION
    assert activity_status(150) is Status.ON_TRACK


def test_weight_status_cases() -> None:
    healthy = target_advice(70, 175, "maintain", 30)
    thin = weight_trend(series([70, 70]), NOW)
    assert weight_status(healthy, thin, "maintain") is Status.ON_TRACK
    under = target_advice(50, 175, "maintain", 30)
    assert weight_status(under, thin, "maintain") is Status.NEEDS_ATTENTION
    over = target_advice(95, 175, "lose", 30)
    assert weight_status(over, thin, "lose") is Status.NOT_ENOUGH_DATA
    down = weight_trend(series([95 - 0.05 * i for i in range(15)]), NOW)  # -0.35 kg a week
    assert weight_status(over, down, "lose") is Status.ON_TRACK
    up = weight_trend(series([95 + 0.05 * i for i in range(15)]), NOW)
    assert weight_status(over, up, "lose") is Status.NEEDS_ATTENTION
    assert weight_status(over, thin, "maintain") is Status.NEEDS_ATTENTION


def test_a_fast_loss_needs_attention_even_in_the_healthy_range() -> None:
    healthy = target_advice(70, 175, "maintain", 30)
    fast = weight_trend(series([70 - 0.2 * i for i in range(15)]), NOW)  # -1.4 kg a week
    assert weight_status(healthy, fast, "maintain") is Status.NEEDS_ATTENTION
