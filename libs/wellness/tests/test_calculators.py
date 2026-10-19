import itertools
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st
from vitalmesh_wellness.calculators import (
    ACTIVITY_FACTOR,
    ActivityLevel,
    OutOfRange,
    Sex,
    bmr_mifflin_st_jeor,
    heart_rate_zones,
    max_heart_rate_tanaka,
    sleep_debt_hours,
    tdee,
)

weights = st.floats(25, 400)
heights = st.floats(100, 250)
ages = st.floats(18, 100)


def test_bmr_reference_values() -> None:
    # Hand-computed from the published equation: 10*80 + 6.25*180 - 5*30 = 1775 (+5 / -161).
    assert bmr_mifflin_st_jeor(Sex.MALE, 80, 180, 30) == pytest.approx(1780.0)
    assert bmr_mifflin_st_jeor(Sex.FEMALE, 80, 180, 30) == pytest.approx(1614.0)


def test_tdee_applies_activity_factor() -> None:
    assert tdee(1780.0, ActivityLevel.MODERATE) == pytest.approx(1780.0 * 1.55)


def test_activity_factors_are_strictly_increasing() -> None:
    factors = [ACTIVITY_FACTOR[level] for level in ActivityLevel]
    assert factors == sorted(factors)
    assert len(set(factors)) == len(factors)


@given(weights, heights, ages)
def test_bmr_male_is_exactly_166_above_female(w: float, h: float, a: float) -> None:
    diff = bmr_mifflin_st_jeor(Sex.MALE, w, h, a) - bmr_mifflin_st_jeor(Sex.FEMALE, w, h, a)
    assert diff == pytest.approx(166.0)


@given(weights, heights, ages, st.floats(0.1, 50))
def test_bmr_increases_with_weight_and_decreases_with_age(
    w: float, h: float, a: float, d: float
) -> None:
    assert bmr_mifflin_st_jeor(Sex.MALE, min(w + d, 400), h, a) >= bmr_mifflin_st_jeor(
        Sex.MALE, w, h, a
    )
    older = min(a + d, 100.0)
    assert bmr_mifflin_st_jeor(Sex.MALE, w, h, older) <= bmr_mifflin_st_jeor(Sex.MALE, w, h, a)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, 0.0, -5.0, 1e9])
def test_bmr_rejects_nonsense_weight(bad: float) -> None:
    with pytest.raises(OutOfRange):
        bmr_mifflin_st_jeor(Sex.MALE, bad, 180, 30)


def test_max_hr_reference() -> None:
    assert max_heart_rate_tanaka(30) == pytest.approx(187.0)


@given(ages, st.floats(30, 120))
def test_zones_are_contiguous_ascending_and_inside_rest_and_max(age: float, rest: float) -> None:
    top = max_heart_rate_tanaka(age)
    if rest >= top:
        with pytest.raises(OutOfRange):
            heart_rate_zones(age, rest)
        return
    zones = heart_rate_zones(age, rest)
    assert [z.zone for z in zones] == [1, 2, 3, 4, 5]
    for lo, hi in itertools.pairwise(zones):
        assert lo.high_bpm == pytest.approx(hi.low_bpm)
    for z in zones:
        assert z.low_bpm < z.high_bpm
    assert zones[-1].high_bpm == pytest.approx(top)
    assert zones[0].low_bpm > rest


def test_zone_reference_values() -> None:
    # age 30, rest 60: HRR = 187 - 60 = 127; zone 3 = 60 + 0.7*127 .. 60 + 0.8*127
    z3 = heart_rate_zones(30, 60)[2]
    assert z3.low_bpm == pytest.approx(148.9)
    assert z3.high_bpm == pytest.approx(161.6)


def test_sleep_debt_reference_and_no_repayment() -> None:
    assert sleep_debt_hours([8, 6, 7, 9, 5]) == pytest.approx(0 + 2 + 1 + 0 + 3)


@given(st.lists(st.floats(0, 24), max_size=30), st.floats(4, 12))
def test_sleep_debt_nonnegative_and_bounded(nights: list[float], need: float) -> None:
    debt = sleep_debt_hours(nights, need)
    assert 0.0 <= debt <= need * len(nights) + 1e-9


def test_sleep_debt_rejects_bad_input() -> None:
    with pytest.raises(OutOfRange):
        sleep_debt_hours([8.0, math.nan])
    with pytest.raises(OutOfRange):
        sleep_debt_hours([8.0] * 31)
    with pytest.raises(OutOfRange):
        sleep_debt_hours([25.0])
