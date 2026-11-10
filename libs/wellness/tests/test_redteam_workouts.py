"""Red team for workouts (D101). Synthetic people only, no model, no network.

The attacks come from the rules, not from what the code happens to do:

* every condition in the knowledge graph that limits intensity, and every condition the graph does
  not know at all, must get easy cardio only, whatever the sex, sleep, place, minutes, focus
  request or spelling of the condition;
* a plan TAMPERED with after generation (a lift added for a cardiac user, an exercise that is not
  in the catalogue, a heavy single, too many sets, a zone above the cap) must be blocked by the
  Guardian, which trusts nothing the planner says;
* nothing the user types can steer the planner outside the catalogue.
"""

from __future__ import annotations

import itertools
from datetime import date, timedelta

import pytest
from vitalmesh_wellness.calculators import ActivityLevel, Sex
from vitalmesh_wellness.claims import Claim, Provenance, Quantity, WorkoutEntry
from vitalmesh_wellness.engine import Profile, build_workout_recommendation
from vitalmesh_wellness.guardian import (
    METRIC_WORKOUT_EXERCISE_SETS,
    METRIC_WORKOUT_TOTAL_SETS,
    METRIC_WORKOUT_ZONE,
    Guardian,
)
from vitalmesh_wellness.knowledge import DEFAULT_INDEX
from vitalmesh_wellness.workouts import BY_ID, Place

START = date(2026, 11, 12)
LIMITING = sorted(DEFAULT_INDEX.limiting_conditions())
UNKNOWN = ["asthma", "knee_injury", "pregnancy", "some_made_up_condition"]
SPELLINGS = (lambda c: c, lambda c: f"  {c.upper()}  ", lambda c: c.title())


def person(*conditions: str, sex: Sex = Sex.FEMALE) -> Profile:
    return Profile(
        user_id="rt-1", sex=sex, age_years=40.0, weight_kg=70.0, height_cm=170.0,
        activity=ActivityLevel.MODERATE, resting_hr_bpm=60.0, conditions=frozenset(conditions),
    )  # fmt: skip


@pytest.mark.parametrize("condition", LIMITING + UNKNOWN)
def test_a_limiting_or_unknown_condition_never_gets_a_lift(condition: str) -> None:
    guardian = Guardian()
    for spell, sex, place, minutes, focus, offset, sleep in itertools.product(
        SPELLINGS,
        Sex,
        Place,
        (10, 30, 90),
        (None, "push", "legs", "full_body"),
        range(4),
        ([8.0] * 7, [5.0] * 7),
    ):
        rec = build_workout_recommendation(
            person(spell(condition), sex=sex),
            sleep,
            "rt",
            START + timedelta(days=offset),
            place,
            minutes,
            focus,
        )
        assert rec.workout_plan == ()
        metrics = {c.quantity.metric for c in rec.claims if c.quantity}
        assert METRIC_WORKOUT_EXERCISE_SETS not in metrics
        assert METRIC_WORKOUT_TOTAL_SETS not in metrics
        zone = next(
            c.quantity.value
            for c in rec.claims
            if c.quantity and c.quantity.metric == METRIC_WORKOUT_ZONE
        )
        cap = DEFAULT_INDEX.max_zone([condition])
        assert zone <= (cap if cap is not None else 3)
        assert guardian.evaluate(rec).cleared


def good_strength(conditions: frozenset[str] = frozenset()):  # type: ignore[no-untyped-def]
    rec = build_workout_recommendation(
        person(), [8.0] * 7, "rt", _push_day(), Place.GYM, 45, "push"
    )
    assert rec.workout_plan
    return rec.model_copy(update={"user": rec.user.model_copy(update={"conditions": conditions})})


def _push_day() -> date:
    return START


def names(rec) -> set[str]:  # type: ignore[no-untyped-def]
    return {v.rule for v in Guardian().evaluate(rec).violations}


@pytest.mark.parametrize("condition", LIMITING + UNKNOWN)
def test_a_lift_added_for_such_a_person_after_generation_is_blocked(condition: str) -> None:
    for spell in SPELLINGS:
        rec = good_strength(frozenset({spell(condition)}))
        assert "workout_safe" in names(rec)


def test_the_untampered_plan_for_a_healthy_person_is_cleared() -> None:
    assert Guardian().evaluate(good_strength()).cleared


def test_tampering_with_the_plan_is_blocked() -> None:
    base = good_strength()
    first = base.workout_plan[0]

    def with_entries(*entries: WorkoutEntry):  # type: ignore[no-untyped-def]
        return base.model_copy(update={"workout_plan": entries})

    cases = {
        "unknown exercise": with_entries(first.model_copy(update={"exercise_id": "snatch_max"})),
        "heavy single": with_entries(first.model_copy(update={"reps_low": 1, "reps_high": 1})),
        "too many reps": with_entries(first.model_copy(update={"reps_high": 50})),
        "too many sets": with_entries(first.model_copy(update={"sets": 12})),
        "zero sets": with_entries(first.model_copy(update={"sets": 0})),
        "negative sets": with_entries(first.model_copy(update={"sets": -3})),
        "two days": with_entries(
            first, first.model_copy(update={"day": START + timedelta(days=1)})
        ),
    }
    for label, rec in cases.items():
        assert "workout_safe" in names(rec), label


def test_tampered_claims_are_blocked() -> None:
    base = good_strength()

    def swap(metric: str, value: float, unit: str):  # type: ignore[no-untyped-def]
        claims = tuple(
            Claim(
                text=c.text, provenance=Provenance.COMPUTED, source=c.source,
                quantity=Quantity(metric=metric, value=value, unit=unit),
            )
            if c.quantity and c.quantity.metric == metric
            else c
            for c in base.claims
        )  # fmt: skip
        return base.model_copy(update={"claims": claims})

    assert "workout_safe" in names(swap("workout_total_sets", 99.0, "sets"))
    assert "workout_safe" in names(swap("workout_minutes", 600.0, "min"))
    assert "units_match" in names(swap("workout_total_sets", 15.0, "reps"))
    assert "units_match" in names(swap("workout_minutes", 45.0, "h"))


def test_a_cardio_zone_above_the_cap_is_blocked_for_every_limiting_condition() -> None:
    for condition in LIMITING:
        cap = DEFAULT_INDEX.max_zone([condition]) or 3
        rec = build_workout_recommendation(person(condition), [8.0] * 7, "rt", START, Place.GYM, 30)
        claims = tuple(
            Claim(
                text=c.text, provenance=Provenance.COMPUTED, source=c.source,
                quantity=Quantity(metric=METRIC_WORKOUT_ZONE, value=cap + 1, unit="zone"),
            )
            if c.quantity and c.quantity.metric == METRIC_WORKOUT_ZONE
            else c
            for c in rec.claims
        )  # fmt: skip
        tampered = rec.model_copy(update={"claims": claims})
        assert not Guardian().evaluate(tampered).cleared, condition


def test_no_session_exercise_is_missing_from_the_catalogue() -> None:
    from vitalmesh_wellness.workouts import SESSIONS

    assert all(i in BY_ID for ids in SESSIONS.values() for i in ids)
