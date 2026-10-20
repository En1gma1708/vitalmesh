from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st
from vitalmesh_wellness.anomaly import MIN_CHANGE, Direction, detect

STEADY_HR = [60, 61, 59, 60, 62, 60, 61, 59, 60]


def test_a_clear_jump_is_flagged_with_direction_and_baseline() -> None:
    a = detect("resting_heart_rate", [*STEADY_HR, 78])
    assert a is not None
    assert (a.direction, a.baseline, a.value) == (Direction.UP, 60, 78)
    down = detect("resting_heart_rate", [*STEADY_HR, 40])
    assert down is not None and down.direction is Direction.DOWN


def test_normal_variation_is_not_flagged() -> None:
    assert detect("resting_heart_rate", [*STEADY_HR, 62]) is None
    assert detect("resting_heart_rate", [*STEADY_HR, 60]) is None


def test_small_change_on_a_flat_series_is_not_flagged() -> None:
    assert detect("body_weight", [70.0] * 10 + [70.4]) is None  # MAD 0 but change below guard


def test_real_change_on_a_flat_series_is_flagged() -> None:
    a = detect("body_weight", [70.0] * 10 + [73.0])
    assert a is not None and math.isinf(a.score)


def test_noisy_series_tolerates_big_swings() -> None:
    noisy = [6.0, 8.5, 5.5, 9.0, 6.5, 8.0, 5.0, 9.5, 7.0]
    assert detect("sleep_duration", [*noisy, 4.5]) is None  # within the usual spread


def test_too_little_history_or_unknown_metric_is_not_flagged() -> None:
    assert detect("resting_heart_rate", [60, 61, 62, 99]) is None
    assert detect("mystery", [1, 1, 1, 1, 1, 1, 1, 1, 100]) is None


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_non_finite_input_is_rejected(bad: float) -> None:
    with pytest.raises(ValueError):
        detect("body_weight", [70.0] * 8 + [bad])


@given(st.floats(min_value=30, max_value=200, allow_nan=False), st.integers(8, 40))
def test_a_constant_series_is_never_flagged(value: float, n: int) -> None:
    assert detect("resting_heart_rate", [value] * n) is None


@given(
    st.lists(st.integers(min_value=50, max_value=70), min_size=8, max_size=30),
    st.integers(min_value=-500, max_value=500),
)
def test_shifting_the_whole_series_does_not_change_the_verdict(
    series: list[int], shift: int
) -> None:
    # integers keep every subtraction exact, so the verdict must be identical, not "close"
    a = detect("resting_heart_rate", [float(v) for v in series])
    b = detect("resting_heart_rate", [float(v + shift) for v in series])
    assert (a is None) == (b is None)
    if a is not None and b is not None:
        assert (a.direction, a.score) == (b.direction, b.score)


def test_registered_metrics_all_have_a_positive_guard() -> None:
    assert all(v > 0 for v in MIN_CHANGE.values())
