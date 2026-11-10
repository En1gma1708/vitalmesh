"""Workouts that follow the person's goal and recent history (D104)."""

from __future__ import annotations

import itertools
from datetime import date, timedelta

import pytest
from vitalmesh_wellness.claims import UserConstraints
from vitalmesh_wellness.engine import Goal, Profile, build_workout
from vitalmesh_wellness.guardian import Guardian
from vitalmesh_wellness.workouts import (
    GOAL_ROTATION,
    MIN_EXERCISES,
    ROTATION,
    SESSIONS,
    Place,
    plan_workout,
)

DAY = date(2026, 11, 12)


def user(*conditions: str) -> UserConstraints:
    return UserConstraints(user_id="u", sex="male", age_years=30, conditions=frozenset(conditions))


def week(place: Place, goal: str) -> list[str]:
    n = len(GOAL_ROTATION[(place, goal)])
    return [
        plan_workout(user(), DAY + timedelta(days=i), place, 45, goal=goal).session
        for i in range(n)
    ]


def profile(goal: Goal = Goal.MAINTAIN, **extra: object) -> Profile:
    base: dict[str, object] = {
        "user_id": "u", "sex": "male", "age_years": 30, "weight_kg": 75, "height_cm": 178,
        "activity": "light", "resting_hr_bpm": 62, "goal": goal,
    }  # fmt: skip
    return Profile.model_validate({**base, **extra})


# ----------------------------------------------------------------------------- goal patterns
def test_every_pattern_uses_only_known_sessions_and_the_base_one_is_unchanged() -> None:
    for (place, _goal), pattern in GOAL_ROTATION.items():
        assert set(pattern) <= set(ROTATION[place]), (place, pattern)
        assert "cardio" in pattern
    assert GOAL_ROTATION[(Place.GYM, "maintain")] == ROTATION[Place.GYM]
    assert GOAL_ROTATION[(Place.HOME, "maintain")] == ROTATION[Place.HOME]


def test_building_muscle_trains_more_days_than_losing_weight_or_maintaining() -> None:
    strength = {
        g: sum(s != "cardio" for s in GOAL_ROTATION[(Place.GYM, g)])
        / len(GOAL_ROTATION[(Place.GYM, g)])
        for g in ("gain", "maintain", "lose")
    }
    assert strength["gain"] > strength["maintain"] >= strength["lose"] - 1e-9
    assert strength["gain"] == pytest.approx(6 / 7)
    assert strength["lose"] == pytest.approx(3 / 6)


def test_losing_weight_alternates_strength_with_easy_cardio() -> None:
    pattern = GOAL_ROTATION[(Place.GYM, "lose")]
    assert all(
        (pattern[i] == "cardio") != (pattern[i + 1] == "cardio") for i in range(len(pattern) - 1)
    )


def test_a_goal_the_table_does_not_know_falls_back_to_the_base_pattern() -> None:
    a = plan_workout(user(), DAY, Place.GYM, 45, goal="lose weight fast")
    b = plan_workout(user(), DAY, Place.GYM, 45, goal="maintain")
    assert a.session == b.session


def test_the_planned_week_follows_the_goal() -> None:
    gain = week(Place.GYM, "gain")
    assert sum(s != "cardio" for s in gain) == 6
    assert {"push", "pull", "legs"} <= set(gain)
    lose = week(Place.GYM, "lose")
    assert sum(s == "cardio" for s in lose) == 3


# ----------------------------------------------------------------------------- no repeat
def test_the_same_muscle_group_is_not_given_two_days_in_a_row() -> None:
    for goal, place in itertools.product(("gain", "lose", "maintain"), Place):
        for offset in range(14):
            d = DAY + timedelta(days=offset)
            first = plan_workout(user(), d, place, 45, goal=goal)
            if first.session == "cardio":
                continue
            second = plan_workout(
                user(), d + timedelta(days=1), place, 45, goal=goal, last_session=first.session
            )
            assert second.session != first.session, (goal, place, offset)


def test_a_repeat_is_replaced_by_the_next_different_session_in_the_pattern() -> None:
    pattern = GOAL_ROTATION[(Place.GYM, "gain")]
    start = next(
        d
        for d in (DAY + timedelta(days=i) for i in range(14))
        if pattern[d.toordinal() % 7] == "push"
    )
    plain = plan_workout(user(), start, Place.GYM, 45, goal="gain")
    assert plain.session == "push"
    moved = plan_workout(user(), start, Place.GYM, 45, goal="gain", last_session="push")
    assert moved.session == "pull"  # the next different entry after push in the gain pattern
    assert moved.why == "rotation"


def test_a_different_last_session_changes_nothing() -> None:
    d = DAY
    base = plan_workout(user(), d, Place.GYM, 45)
    other = "cardio" if base.session != "cardio" else "legs"
    assert plan_workout(user(), d, Place.GYM, 45, last_session=other).session == base.session


def test_an_explicit_request_is_honoured_even_after_the_same_session() -> None:
    plan = plan_workout(user(), DAY, Place.GYM, 45, focus="push", last_session="push")
    assert (plan.session, plan.why) == ("push", "requested")


def test_a_home_pattern_with_two_full_body_days_in_a_row_inserts_cardio() -> None:
    d = next(
        d
        for d in (DAY + timedelta(days=i) for i in range(6))
        if GOAL_ROTATION[(Place.HOME, "gain")][d.toordinal() % 3] == "full_body"
    )
    plan = plan_workout(user(), d, Place.HOME, 45, goal="gain", last_session="full_body")
    assert plan.session == "cardio"


# ----------------------------------------------------------------------------- ease in
def test_an_inactive_week_starts_with_the_minimum_number_of_exercises() -> None:
    d = next(
        d
        for d in (DAY + timedelta(days=i) for i in range(4))
        if plan_workout(user(), d, Place.GYM, 60).session == "push"
    )
    full = plan_workout(user(), d, Place.GYM, 60)
    eased = plan_workout(user(), d, Place.GYM, 60, ease_in=True)
    assert len(full.entries) == len(SESSIONS["push"]) and not full.eased
    assert len(eased.entries) == MIN_EXERCISES and eased.eased
    assert [e.exercise_id for e in eased.entries] == list(SESSIONS["push"])[:MIN_EXERCISES]


def test_ease_in_never_overrides_a_safety_choice() -> None:
    plan = plan_workout(user("cardiac"), DAY, Place.GYM, 60, ease_in=True)
    assert plan.session == "cardio" and plan.why == "conditions" and not plan.eased


# ----------------------------------------------------------------------------- engine and guardian
def test_the_goal_reaches_the_planner_through_the_profile() -> None:
    d = DAY
    sessions = {
        goal: build_workout(profile(goal), [8.0] * 7, "r", d + timedelta(days=i), Place.GYM, 45)[
            1
        ].session
        for goal in Goal
        for i in range(0, 1)
    }
    assert set(sessions) == set(Goal)
    gain_days = [
        build_workout(profile(Goal.GAIN), [8.0] * 7, "r", d + timedelta(days=i), Place.GYM, 45)[
            1
        ].session
        for i in range(7)
    ]
    lose_days = [
        build_workout(profile(Goal.LOSE), [8.0] * 7, "r", d + timedelta(days=i), Place.GYM, 45)[
            1
        ].session
        for i in range(6)
    ]
    assert gain_days.count("cardio") == 1 and lose_days.count("cardio") == 3


def test_the_eased_start_is_said_in_the_text() -> None:
    d = next(
        d
        for d in (DAY + timedelta(days=i) for i in range(4))
        if plan_workout(user(), d, Place.GYM, 45).session == "push"
    )
    rec, plan = build_workout(profile(), [8.0] * 7, "r", d, Place.GYM, 45, ease_in=True)
    assert plan.eased
    assert any("fairly inactive lately, so this starts light" in c.text for c in rec.claims)
    rec2, _ = build_workout(profile(), [8.0] * 7, "r", d, Place.GYM, 45)
    assert not any("starts light" in c.text for c in rec2.claims)


def test_every_goal_history_and_ease_combination_is_cleared_by_the_guardian() -> None:
    guardian = Guardian()
    count = 0
    for goal, place, offset, last, eased, minutes in itertools.product(
        Goal,
        Place,
        range(7),
        (None, "push", "pull", "legs", "full_body", "cardio"),
        (False, True),
        (10, 25, 45, 90),
    ):
        rec, _ = build_workout(
            profile(goal), [8.0] * 7, "r", DAY + timedelta(days=offset), place, minutes,
            last_session=last, ease_in=eased,
        )  # fmt: skip
        verdict = guardian.evaluate(rec)
        assert verdict.cleared, (goal, place, offset, last, eased, minutes, verdict.violations)
        count += 1
    assert count == 3 * 2 * 7 * 6 * 2 * 4


@pytest.mark.parametrize("condition", ["cardiac", "uncontrolled_hypertension", "asthma"])
def test_goal_history_and_ease_in_cannot_unlock_a_lift_for_a_restricted_person(
    condition: str,
) -> None:
    for goal, place, last, eased, offset in itertools.product(
        Goal, Place, (None, "push", "legs"), (False, True), range(7)
    ):
        rec, plan = build_workout(
            profile(goal, conditions={condition}),
            [8.0] * 7,
            "r",
            DAY + timedelta(days=offset),
            place,
            90,
            focus="push",
            last_session=last,
            ease_in=eased,
        )
        assert plan.session == "cardio" and rec.workout_plan == ()
