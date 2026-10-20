from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st
from vitalmesh_wellness.trends import monday_of, trailing_average, weekly_totals

D0 = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)  # a Monday


def pts(values: Sequence[float], step_days: float = 1.0) -> list[tuple[datetime, float]]:
    return [(D0 + timedelta(days=i * step_days), v) for i, v in enumerate(values)]


def test_trailing_average_matches_a_brute_force_window() -> None:
    series = pts([70, 71, 69, 72, 73, 70, 68, 74, 75, 71])
    got = trailing_average(series, 7)
    for i, (t, avg) in enumerate(got):
        window = [v for tt, v in series[: i + 1] if tt > t - timedelta(days=7)]
        assert avg == pytest.approx(sum(window) / len(window))


def test_window_is_by_time_not_by_count() -> None:
    sparse = pts([60, 80, 100], step_days=4)  # points 4 days apart: a 7-day window holds two
    got = [v for _, v in trailing_average(sparse, 7)]
    assert got == [60, 70, 90]


def test_bad_input_is_rejected() -> None:
    with pytest.raises(ValueError):
        trailing_average(pts([1.0]), 0)
    with pytest.raises(ValueError):
        trailing_average(pts([1.0, float("nan")]), 7)
    assert trailing_average([], 7) == []


@given(st.lists(st.floats(min_value=0, max_value=1000, allow_nan=False), min_size=1, max_size=60))
def test_average_stays_within_the_window_bounds(values: list[float]) -> None:
    series = pts(values)
    for (_, avg), (_, _) in zip(trailing_average(series, 7), series, strict=True):
        assert min(values) - 1e-9 <= avg <= max(values) + 1e-9


@given(st.floats(min_value=0, max_value=1000, allow_nan=False), st.integers(1, 40))
def test_constant_series_has_that_constant_as_its_average(value: float, n: int) -> None:
    assert all(a == pytest.approx(value) for _, a in trailing_average(pts([value] * n), 7))


def test_weekly_totals_group_by_monday_start() -> None:
    series = pts([30] * 7 + [10] * 3)  # Mon 5 Oct .. Sun 11 Oct, then Mon 12 .. Wed 14
    assert weekly_totals(series) == [(date(2026, 10, 5), 210), (date(2026, 10, 12), 30)]
    assert monday_of(date(2026, 10, 11)) == date(2026, 10, 5)
    assert weekly_totals([]) == []


@given(st.lists(st.integers(0, 600), min_size=1, max_size=100))
def test_weekly_totals_conserve_the_sum(values: list[int]) -> None:
    series = pts([float(v) for v in values])
    assert sum(t for _, t in weekly_totals(series)) == pytest.approx(sum(values))
