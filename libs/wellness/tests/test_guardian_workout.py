"""The Guardian rule ``workout_safe`` and the workout metrics, at their exact boundaries (D101).

Each bound is tested on both sides so a mutated comparison cannot survive. The entry-level checks
re-derive everything from the catalogue and never trust the planner.
"""

from __future__ import annotations

from datetime import date

import pytest
from vitalmesh_wellness import guardian as guardian_module
from vitalmesh_wellness.claims import (
    Claim,
    Provenance,
    Quantity,
    Recommendation,
    UserConstraints,
    WorkoutEntry,
)
from vitalmesh_wellness.guardian import (
    METRIC_ENERGY,
    METRIC_WORKOUT_EXERCISE_SETS,
    METRIC_WORKOUT_MINUTES,
    METRIC_WORKOUT_TOTAL_SETS,
    METRIC_WORKOUT_ZONE,
    RULES_VERSION,
    Guardian,
    rule_workout_safe,
)
from vitalmesh_wellness.knowledge import ConstraintIndex, Edge
from vitalmesh_wellness.workouts import (
    MAX_EXERCISES,
    MAX_MINUTES,
    MAX_SETS_PER_EXERCISE,
    MAX_TOTAL_SETS,
    MIN_MINUTES,
)

DAY = date(2026, 11, 12)
OTHER_DAY = date(2026, 11, 13)


def claim(metric: str, value: float, unit: str) -> Claim:
    return Claim(
        text=f"{metric} {value:g}",
        provenance=Provenance.COMPUTED,
        source="test",
        quantity=Quantity(metric=metric, value=value, unit=unit),
    )


def minutes(v: float) -> Claim:
    return claim(METRIC_WORKOUT_MINUTES, v, "min")


def zone(v: float) -> Claim:
    return claim(METRIC_WORKOUT_ZONE, v, "zone")


def sets(v: float) -> Claim:
    return claim(METRIC_WORKOUT_EXERCISE_SETS, v, "sets")


def total(v: float) -> Claim:
    return claim(METRIC_WORKOUT_TOTAL_SETS, v, "sets")


def entry(
    exercise: str = "bench_press",
    n_sets: int = 3,
    low: int = 8,
    high: int = 12,
    day: date = DAY,
) -> WorkoutEntry:
    return WorkoutEntry(day=day, exercise_id=exercise, sets=n_sets, reps_low=low, reps_high=high)


def rec(
    claims: tuple[Claim, ...] = (),
    plan: tuple[WorkoutEntry, ...] = (),
    conditions: frozenset[str] = frozenset(),
) -> Recommendation:
    user = UserConstraints(user_id="u", sex="male", age_years=30, conditions=conditions)
    return Recommendation(recommendation_id="r", user=user, claims=claims, workout_plan=plan)


def rules(r: Recommendation) -> list[str]:
    return [v.rule for v in rule_workout_safe(r)]


def blocked(r: Recommendation) -> bool:
    return bool(rule_workout_safe(r))


def messages(r: Recommendation) -> str:
    return " | ".join(v.message for v in rule_workout_safe(r))


def test_rules_version_was_bumped_for_the_new_rule() -> None:
    assert RULES_VERSION == "4"
    assert rule_workout_safe in guardian_module.DEFAULT_RULES


def test_a_recommendation_with_no_workout_is_not_touched() -> None:
    assert rule_workout_safe(rec((claim(METRIC_ENERGY, 2000, "kcal"),))) == []


# ----------------------------------------------------------------------------- minutes and zone
@pytest.mark.parametrize(
    ("value", "bad"),
    [
        (MIN_MINUTES, False),
        (MIN_MINUTES - 0.01, True),
        (MAX_MINUTES, False),
        (MAX_MINUTES + 0.01, True),
    ],
)
def test_minutes_bounds_are_exact(value: float, bad: bool) -> None:
    r = rec((minutes(value),))
    assert blocked(r) is bad
    if bad:
        assert "min" in messages(r)


@pytest.mark.parametrize(
    ("value", "bad"), [(1, False), (3, False), (0.99, True), (3.01, True), (0, True)]
)
def test_cardio_zone_bounds_are_exact(value: float, bad: bool) -> None:
    assert blocked(rec((zone(value),))) is bad


def test_the_intensity_cap_covers_the_workout_zone_too(monkeypatch: pytest.MonkeyPatch) -> None:
    # a strict synthetic cap (zone 2) so the check cannot be satisfied by the beginner ceiling
    index = ConstraintIndex([Edge("limits_intensity", "xcond", "2")])
    monkeypatch.setattr(guardian_module, "DEFAULT_INDEX", index)
    r = rec((zone(3),), conditions=frozenset({"xcond"}))
    names = [v.rule for v in Guardian().evaluate(r).violations]
    assert "intensity_cap" in names
    assert "intensity_cap" not in [
        v.rule
        for v in Guardian().evaluate(rec((zone(2),), conditions=frozenset({"xcond"}))).violations
    ]


def test_units_are_enforced_for_every_workout_metric() -> None:
    wrong = (
        claim(METRIC_WORKOUT_MINUTES, 30, "h"),
        claim(METRIC_WORKOUT_ZONE, 2, "bpm"),
        claim(METRIC_WORKOUT_EXERCISE_SETS, 3, "reps"),
        claim(METRIC_WORKOUT_TOTAL_SETS, 3, "reps"),
    )
    violations = Guardian().evaluate(rec(wrong)).violations
    assert [v.rule for v in violations].count("units_match") == 4


# ----------------------------------------------------------------------------- strength claims
def test_a_consistent_strength_session_is_cleared() -> None:
    assert not blocked(rec((minutes(45), sets(3), sets(3), total(6))))


@pytest.mark.parametrize(
    ("value", "bad"),
    [
        (1, False),
        (MAX_SETS_PER_EXERCISE, False),
        (0, True),
        (MAX_SETS_PER_EXERCISE + 1, True),
        (2.5, True),
    ],
)
def test_sets_per_exercise_bounds_and_whole_numbers(value: float, bad: bool) -> None:
    assert blocked(rec((sets(value), total(value)))) is bad


@pytest.mark.parametrize(("n_total", "bad"), [(MAX_TOTAL_SETS, False), (MAX_TOTAL_SETS + 1, True)])
def test_total_sets_bound_is_exact(n_total: int, bad: bool) -> None:
    # five exercises of five sets = 25; four of five and one of four = 24
    per = [5.0] * 4 + [4.0 if n_total == MAX_TOTAL_SETS else 5.0]
    r = rec((*(sets(v) for v in per), total(sum(per))))
    assert sum(per) == n_total
    assert blocked(r) is bad
    if bad:
        assert "total sets" in messages(r)


def test_the_total_must_equal_the_exercises() -> None:
    assert "add up" in messages(rec((sets(3), sets(3), total(7))))
    assert "add up" in messages(rec((sets(3), sets(3), total(5))))
    assert not blocked(rec((sets(3), sets(3), total(6))))


def test_a_strength_claim_needs_exactly_one_total() -> None:
    assert "exactly one total" in messages(rec((sets(3),)))
    assert "exactly one total" in messages(rec((sets(3), total(3), total(3))))


def test_a_lonely_total_is_checked_on_its_own_bound() -> None:
    assert not blocked(rec((total(MAX_TOTAL_SETS),)))
    assert blocked(rec((total(MAX_TOTAL_SETS + 1),)))


def test_the_number_of_exercises_is_bounded() -> None:
    nine = (*(sets(1) for _ in range(MAX_EXERCISES + 1)), total(MAX_EXERCISES + 1))
    eight = (*(sets(1) for _ in range(MAX_EXERCISES)), total(MAX_EXERCISES))
    assert "exercises" in messages(rec(nine))
    assert not blocked(rec(eight))


# ----------------------------------------------------------------------------- conditions
@pytest.mark.parametrize(
    ("conditions", "strength_ok"),
    [
        (frozenset(), True),
        (frozenset({"celiac_disease"}), True),
        (frozenset({"cardiac"}), False),
        (frozenset({"uncontrolled_hypertension"}), False),
        (frozenset({"asthma"}), False),  # unknown to the graph: never assumed safe
        (frozenset({"celiac_disease", "asthma"}), False),
    ],
)
def test_strength_work_depends_on_the_declared_conditions(
    conditions: frozenset[str], strength_ok: bool
) -> None:
    by_claims = rec((sets(3), total(3)), conditions=conditions)
    by_plan = rec((total(3),), plan=(entry(),), conditions=conditions)
    assert blocked(by_claims) is (not strength_ok)
    assert blocked(by_plan) is (not strength_ok)
    if not strength_ok:
        assert "conditions this user declared" in messages(by_claims)


def test_easy_cardio_is_always_allowed_whatever_the_conditions() -> None:
    for conditions in (frozenset({"cardiac"}), frozenset({"asthma"}), frozenset()):
        assert not blocked(rec((minutes(30), zone(2)), conditions=conditions))


# ----------------------------------------------------------------------------- entries
def test_a_catalogue_entry_is_cleared() -> None:
    assert not blocked(rec((total(3),), plan=(entry(),)))


def test_an_unknown_exercise_is_blocked() -> None:
    assert "unknown exercise" in messages(rec((total(3),), plan=(entry("deadlift_max_effort"),)))


@pytest.mark.parametrize(
    ("n_sets", "bad"), [(1, False), (MAX_SETS_PER_EXERCISE, False), (0, True), (6, True)]
)
def test_entry_sets_bounds_are_exact(n_sets: int, bad: bool) -> None:
    assert blocked(rec((total(n_sets),), plan=(entry(n_sets=n_sets),))) is bad


@pytest.mark.parametrize(
    ("low", "high", "bad"),
    [
        (8, 12, False),  # exactly the catalogue range
        (9, 11, False),  # inside it
        (8, 8, False),
        (12, 12, False),
        (7, 12, True),  # below the catalogue minimum
        (8, 13, True),  # above the catalogue maximum
        (10, 9, True),  # inverted
        (1, 1, True),  # a heavy single is not in this catalogue
    ],
)
def test_entry_reps_must_sit_inside_the_catalogue_range(low: int, high: int, bad: bool) -> None:
    assert blocked(rec((total(3),), plan=(entry(low=low, high=high),))) is bad


def test_the_volume_of_one_day_is_bounded() -> None:
    ok = (*(entry(n_sets=5) for _ in range(4)), entry(n_sets=4))  # 24 sets
    too_many_sets = tuple(entry(n_sets=5) for _ in range(5))  # 25 sets
    assert not blocked(rec((total(24),), plan=ok))
    assert "too much volume" in messages(rec((total(25),), plan=too_many_sets))
    nine_exercises = tuple(entry(n_sets=1) for _ in range(MAX_EXERCISES + 1))
    assert "too much volume" in messages(rec((total(9),), plan=nine_exercises))
    eight_exercises = tuple(entry(n_sets=1) for _ in range(MAX_EXERCISES))
    assert not blocked(rec((total(8),), plan=eight_exercises))


def test_a_workout_covers_one_day() -> None:
    two_days = (entry(n_sets=3, day=DAY), entry(n_sets=3, day=OTHER_DAY))
    assert "covers one day" in messages(rec((total(6),), plan=two_days))
    same_day = (entry(n_sets=3, day=DAY), entry(n_sets=3, day=DAY))
    assert not blocked(rec((total(6),), plan=same_day))


def test_the_plan_must_match_the_claimed_total() -> None:
    assert "plan has 3 sets" in messages(rec((total(4),), plan=(entry(),)))
    assert not blocked(rec((total(3),), plan=(entry(),)))


def test_the_checks_are_independent_of_each_other() -> None:
    r = rec(
        (minutes(5), zone(4), sets(9), total(30)),
        plan=(entry("nope", 9, 1, 99),),
        conditions=frozenset({"cardiac"}),
    )
    found = messages(r)
    for fragment in (
        "conditions this user declared",
        "min",
        "zone",
        "sets",
        "total sets",
        "unknown exercise",
    ):
        assert fragment in found
