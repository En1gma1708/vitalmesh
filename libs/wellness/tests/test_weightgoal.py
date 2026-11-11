"""Unsafe weight requests are declined; ordinary ones are not (D108 step one)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from vitalmesh_wellness.weightgoal import (
    UNDERWEIGHT_BMI,
    bmi,
    loss_per_week_kg,
    target_weight_kg,
    unsafe_weight_request,
)

H = 175.0  # BMI 18.5 at 175 cm is 56.7 kg


@pytest.mark.parametrize(
    "question",
    [
        "i want to weigh 40 kg, how do i get there?",
        "how do I get down to 45 kg",
        "i want to be 50 kilos",
        "help me reach 100 lbs",
        "my goal is to weigh 90 pounds",
        "Can I weigh 38kg?",
    ],
)
def test_underweight_targets_are_declined_for_a_175_cm_adult(question: str) -> None:
    assert unsafe_weight_request(question, height_cm=H) == "target_below_healthy"


@pytest.mark.parametrize(
    "question",
    [
        "lose 10 kg in a week",
        "i want to lose 5 kg in 3 days",
        "lose 8 kg per week",
        "drop 20 pounds in a week",
        "shed 4 kg in 2 days",
    ],
)
def test_too_fast_losses_are_declined(question: str) -> None:
    assert unsafe_weight_request(question, height_cm=H, weight_kg=80) == "loss_too_fast"


@pytest.mark.parametrize(
    "question",
    ["how low can i go", "how low can I get my weight", "what is the lowest weight i can reach"],
)
def test_how_low_is_declined(question: str) -> None:
    assert unsafe_weight_request(question, height_cm=H) == "how_low"


@pytest.mark.parametrize(
    "question",
    [
        "how am i doing",
        "how is my weight",
        "what should my target weight be",
        "i want to weigh 70 kg",
        "i want to lose 0.5 kg a week",
        "lose 2 kg in 4 weeks",
        "lose 0.7 kg per week",
        "how was my sleep",
        "i slept 7 hours, how do i get to 8",
        "what should i eat to lose weight",
        "i weigh 82 kg",
    ],
)
def test_ordinary_questions_are_not_declined(question: str) -> None:
    assert unsafe_weight_request(question, height_cm=H, weight_kg=80) is None


def test_the_target_depends_on_height() -> None:
    # 50 kg is BMI 16.3 at 175 cm, but 21.5 at 152 cm
    assert unsafe_weight_request("i want to weigh 50 kg", height_cm=175) == "target_below_healthy"
    assert unsafe_weight_request("i want to weigh 50 kg", height_cm=152) is None


def test_without_a_height_only_an_absolute_floor_applies() -> None:
    assert unsafe_weight_request("i want to weigh 38 kg") == "target_below_healthy"
    assert unsafe_weight_request("i want to weigh 50 kg") is None


def test_the_weekly_cap_is_one_percent_of_body_weight_for_a_light_person() -> None:
    # 0.9 kg a week is under 1 kg but over 1 percent of a 60 kg person (0.6 kg)
    assert unsafe_weight_request("lose 0.9 kg a week", weight_kg=60) == "loss_too_fast"
    assert unsafe_weight_request("lose 0.9 kg a week", weight_kg=100) is None


def test_parsers_convert_units() -> None:
    assert target_weight_kg("i want to weigh 100 lbs") == pytest.approx(45.36, abs=0.01)
    assert loss_per_week_kg("lose 4 kg in 2 weeks") == pytest.approx(2.0)
    assert loss_per_week_kg("lose 3 kg a month") == pytest.approx(0.7, abs=0.01)
    assert loss_per_week_kg("lose 3 kg") is None


@given(
    height=st.floats(min_value=120, max_value=230),
    target=st.floats(min_value=30, max_value=200),
)
def test_target_rule_is_exactly_the_bmi_line(height: float, target: float) -> None:
    got = unsafe_weight_request(f"i want to weigh {target:.1f} kg", height_cm=height)
    # use the value the parser saw, after the one-decimal formatting
    seen = float(f"{target:.1f}")
    assert (got is not None) == (bmi(seen, height) < UNDERWEIGHT_BMI)
