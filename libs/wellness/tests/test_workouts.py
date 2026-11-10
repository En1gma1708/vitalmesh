"""The workout catalogue and planner (D101): concrete, deterministic, safe by construction."""

from __future__ import annotations

import itertools
import re
from datetime import date, timedelta

import pytest
from vitalmesh_wellness.claims import UserConstraints
from vitalmesh_wellness.engine import Profile, build_workout_recommendation
from vitalmesh_wellness.guardian import (
    METRIC_WORKOUT_EXERCISE_SETS,
    METRIC_WORKOUT_MINUTES,
    METRIC_WORKOUT_TOTAL_SETS,
    METRIC_WORKOUT_ZONE,
    Guardian,
)
from vitalmesh_wellness.workouts import (
    BY_ID,
    CATALOGUE,
    MAX_EXERCISES,
    MAX_MINUTES,
    MAX_SETS_PER_EXERCISE,
    MIN_EXERCISES,
    MIN_MINUTES,
    MIN_STRENGTH_MINUTES,
    ROTATION,
    SESSIONS,
    Place,
    WorkoutError,
    exercise_count,
    plan_workout,
    strength_allowed,
)

DAY = date(2026, 11, 12)


def user(*conditions: str) -> UserConstraints:
    return UserConstraints(user_id="u", sex="male", age_years=30, conditions=frozenset(conditions))


def profile(**extra: object) -> Profile:
    base: dict[str, object] = {
        "user_id": "u", "sex": "male", "age_years": 30, "weight_kg": 75, "height_cm": 178,
        "activity": "light", "resting_hr_bpm": 62,
    }  # fmt: skip
    return Profile.model_validate({**base, **extra})


def day_with(place: Place, session: str) -> date:
    for offset in range(8):
        d = DAY + timedelta(days=offset)
        if plan_workout(user(), d, place, 45).session == session:
            return d
    raise AssertionError(f"no day in a week gives {session}")


# ----------------------------------------------------------------------------- the catalogue
def test_catalogue_ids_are_unique_and_indexed() -> None:
    assert len({e.id for e in CATALOGUE}) == len(CATALOGUE)
    assert set(BY_ID) == {e.id for e in CATALOGUE}


@pytest.mark.parametrize("ex", CATALOGUE, ids=lambda e: e.id)
def test_every_exercise_is_a_beginner_prescription(ex) -> None:  # type: ignore[no-untyped-def]
    assert 1 <= ex.sets <= MAX_SETS_PER_EXERCISE
    assert 1 <= ex.reps_low <= ex.reps_high
    assert ex.unit in ("reps", "seconds")
    assert ex.places  # usable somewhere
    assert not re.search(r"\d", ex.cue), "a number must come from a claim, never from a cue"
    assert ex.name.strip() and ex.cue.strip()


def test_no_exercise_asks_for_a_maximal_lift() -> None:
    # a beginner catalogue: nothing is prescribed as a one-rep-max or a single heavy rep
    assert all(e.reps_low >= 6 or e.unit == "seconds" for e in CATALOGUE)
    assert not any("max" in e.name.lower() or "1rm" in e.name.lower() for e in CATALOGUE)


def test_sessions_only_use_known_exercises_and_fit_their_place() -> None:
    for name, ids in SESSIONS.items():
        assert len(ids) >= MIN_EXERCISES, name
        assert len(set(ids)) == len(ids), name
        for i in ids:
            assert i in BY_ID, (name, i)
    for i in SESSIONS["full_body"]:  # the home session needs no gym equipment at all
        assert Place.HOME in BY_ID[i].places, i


def test_the_named_lifts_the_user_asked_for_exist() -> None:
    assert "bench_press" in SESSIONS["push"]
    assert BY_ID["bench_press"].name == "Bench press"


# ----------------------------------------------------------------------------- strength_allowed
@pytest.mark.parametrize(
    ("conditions", "allowed"),
    [
        ((), True),
        (("celiac_disease",), True),  # known to the graph, says nothing about exercise
        (("  Celiac_Disease ",), True),  # case and space folded
        (("cardiac",), False),  # limits intensity
        (("uncontrolled_hypertension",), False),
        (("asthma",), False),  # unknown to the graph: never assumed safe
        (("celiac_disease", "asthma"), False),
        (("",), True),  # a blank entry is nothing
    ],
)
def test_strength_allowed(conditions: tuple[str, ...], allowed: bool) -> None:
    assert strength_allowed(frozenset(conditions)) is allowed


# ----------------------------------------------------------------------------- exercise_count
@pytest.mark.parametrize(
    ("minutes", "available", "expected"),
    [
        (25, 5, 3),
        (30, 5, 3),
        (33, 5, 4),
        (39, 5, 4),
        (40, 5, 5),
        (45, 5, 5),
        (90, 5, 5),
        (90, 7, 7),
        (90, 20, MAX_EXERCISES),
    ],
)
def test_exercise_count(minutes: int, available: int, expected: int) -> None:
    assert exercise_count(minutes, available) == expected
    assert MIN_EXERCISES <= exercise_count(minutes, available) <= MAX_EXERCISES


# ----------------------------------------------------------------------------- planning
def test_same_inputs_give_the_same_plan() -> None:
    a = plan_workout(user(), DAY, Place.GYM, 45)
    assert a == plan_workout(user(), DAY, Place.GYM, 45)


def test_the_gym_rotation_visits_every_session_in_four_days() -> None:
    seen = {plan_workout(user(), DAY + timedelta(days=i), Place.GYM, 45).session for i in range(4)}
    assert seen == set(ROTATION[Place.GYM]) == {"push", "pull", "legs", "cardio"}


def test_the_home_rotation_alternates_full_body_and_cardio() -> None:
    seen = [plan_workout(user(), DAY + timedelta(days=i), Place.HOME, 45).session for i in range(4)]
    assert set(seen) == {"full_body", "cardio"}
    assert seen[0] == seen[2] != seen[1] == seen[3]


def test_a_push_day_names_the_bench_press_with_sets_and_reps() -> None:
    plan = plan_workout(user(), day_with(Place.GYM, "push"), Place.GYM, 45)
    assert plan.why == "rotation" and plan.cardio_zone is None
    first = plan.entries[0]
    assert (first.exercise_id, first.sets, first.reps_low, first.reps_high) == (
        "bench_press",
        3,
        8,
        12,
    )
    assert [e.exercise_id for e in plan.entries] == list(SESSIONS["push"])  # 45 min fits all five


def test_a_cardio_day_is_easy_zone_two_for_the_asked_minutes() -> None:
    plan = plan_workout(user(), day_with(Place.GYM, "cardio"), Place.GYM, 30)
    assert (plan.session, plan.minutes, plan.cardio_zone, plan.entries) == ("cardio", 30, 2, ())


def test_home_never_includes_gym_only_exercises() -> None:
    for offset, minutes in itertools.product(range(4), (25, 45, 90)):
        plan = plan_workout(user(), DAY + timedelta(days=offset), Place.HOME, minutes)
        for e in plan.entries:
            assert Place.HOME in BY_ID[e.exercise_id].places


def test_minutes_bounds() -> None:
    for bad in (MIN_MINUTES - 1, MAX_MINUTES + 1, 0, -5):
        with pytest.raises(WorkoutError):
            plan_workout(user(), DAY, Place.GYM, bad)
    for ok in (MIN_MINUTES, MAX_MINUTES):
        assert plan_workout(user(), DAY, Place.GYM, ok).minutes == ok


def test_too_little_time_for_strength_gives_easy_cardio() -> None:
    d = day_with(Place.GYM, "push")
    short = plan_workout(user(), d, Place.GYM, MIN_STRENGTH_MINUTES - 1)
    assert (short.session, short.why, short.cardio_zone) == ("cardio", "short", 2)
    exactly = plan_workout(user(), d, Place.GYM, MIN_STRENGTH_MINUTES)
    assert exactly.session == "push" and len(exactly.entries) == MIN_EXERCISES


def test_a_requested_focus_is_honoured_when_the_place_offers_it() -> None:
    d = day_with(Place.GYM, "cardio")
    plan = plan_workout(user(), d, Place.GYM, 45, focus="legs")
    assert (plan.session, plan.why) == ("legs", "requested")
    assert plan_workout(user(), d, Place.GYM, 45, focus=" PUSH ").session == "push"


def test_a_focus_the_place_does_not_offer_falls_back_to_the_rotation() -> None:
    d = day_with(Place.HOME, "full_body")
    plan = plan_workout(user(), d, Place.HOME, 45, focus="push")  # no push day at home
    assert (plan.session, plan.why) == ("full_body", "rotation")
    assert plan_workout(user(), d, Place.GYM, 45, focus="nonsense").why == "rotation"


def test_a_sleep_debt_day_is_easy_cardio_even_on_a_lifting_day() -> None:
    d = day_with(Place.GYM, "push")
    plan = plan_workout(user(), d, Place.GYM, 45, recovering=True, focus="push")
    assert (plan.session, plan.why, plan.cardio_zone, plan.entries) == ("cardio", "recovery", 2, ())


@pytest.mark.parametrize("conditions", [("cardiac",), ("uncontrolled_hypertension",), ("asthma",)])
def test_a_limiting_or_unknown_condition_gets_easy_cardio_only(conditions: tuple[str, ...]) -> None:
    for place, offset in itertools.product(Place, range(4)):
        plan = plan_workout(
            user(*conditions), DAY + timedelta(days=offset), place, 60, focus="push"
        )
        assert (plan.session, plan.why, plan.cardio_zone, plan.entries) == (
            "cardio",
            "conditions",
            2,
            (),
        )


def test_a_condition_that_says_nothing_about_exercise_does_not_restrict() -> None:
    plan = plan_workout(user("celiac_disease"), day_with(Place.GYM, "push"), Place.GYM, 45)
    assert plan.session == "push"


# ----------------------------------------------------------------------------- the claims
def metrics(rec) -> dict[str, list[float]]:  # type: ignore[no-untyped-def]
    out: dict[str, list[float]] = {}
    for c in rec.claims:
        if c.quantity:
            out.setdefault(c.quantity.metric, []).append(c.quantity.value)
    return out


def test_a_strength_plan_is_claims_with_exact_texts() -> None:
    d = day_with(Place.GYM, "push")
    rec = build_workout_recommendation(profile(), [8.0] * 7, "r", d, Place.GYM, 45)
    texts = [c.text for c in rec.claims if c.quantity and c.quantity.metric.startswith("workout")]
    assert texts[0] == (
        "Today: push day (chest, shoulders, triceps) at the gym, about 45 minutes including a "
        "5-minute easy warm-up."
    )
    assert "Bench press: 3 sets of 8-12 reps." in texts
    assert "Plank: 3 sets of 20-40 seconds." in texts
    assert texts[-1].startswith("That is 15 sets in total.")
    m = metrics(rec)
    assert m[METRIC_WORKOUT_MINUTES] == [45.0]
    assert m[METRIC_WORKOUT_TOTAL_SETS] == [15.0]
    assert m[METRIC_WORKOUT_EXERCISE_SETS] == [3.0] * 5
    assert len(rec.workout_plan) == 5
    assert METRIC_WORKOUT_ZONE not in m


def test_a_cardio_plan_names_minutes_and_the_zone_two_heart_rate_range() -> None:
    d = day_with(Place.GYM, "cardio")
    rec = build_workout_recommendation(profile(), [8.0] * 7, "r", d, Place.GYM, 30)
    texts = [c.text for c in rec.claims if c.quantity and c.quantity.metric.startswith("workout")]
    assert texts[0] == (
        "Today: 30 minutes of easy cardio (a brisk walk, cycling or a light jog), at a pace "
        "where you can still talk."
    )
    assert texts[1] == "Easy pace is zone 2: 137-150 bpm."
    m = metrics(rec)
    assert m[METRIC_WORKOUT_ZONE] == [2.0]
    assert rec.workout_plan == ()


def test_why_a_plan_is_easy_is_said_in_the_text() -> None:
    d = day_with(Place.GYM, "push")
    debt = build_workout_recommendation(profile(), [5.0] * 7, "r", d, Place.GYM, 30)
    assert any("recent sleep debt" in c.text for c in debt.claims)
    ill = build_workout_recommendation(
        profile(conditions={"cardiac"}), [8.0] * 7, "r", d, Place.GYM, 30
    )
    assert any("check with your doctor" in c.text for c in ill.claims)
    short = build_workout_recommendation(profile(), [8.0] * 7, "r", d, Place.GYM, 20)
    assert any("this little time" in c.text for c in short.claims)


def test_every_plan_the_planner_makes_is_cleared_by_the_guardian() -> None:
    guardian = Guardian()
    sleeps = ([8.0] * 7, [5.0] * 7)
    conds = ((), ("cardiac",), ("asthma",), ("celiac_disease",))
    count = 0
    for place, minutes, offset, sleep, cond in itertools.product(
        Place,
        (MIN_MINUTES, 20, MIN_STRENGTH_MINUTES, 30, 45, 60, MAX_MINUTES),
        range(8),
        sleeps,
        conds,
    ):
        rec = build_workout_recommendation(
            profile(conditions=set(cond)), sleep, "r", DAY + timedelta(days=offset), place, minutes
        )
        verdict = guardian.evaluate(rec)
        assert verdict.cleared, (place, minutes, offset, cond, verdict.violations)
        count += 1
    assert count == 2 * 7 * 8 * 2 * 4
