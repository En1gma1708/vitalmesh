import pytest
from hypothesis import given
from hypothesis import strategies as st
from vitalmesh_wellness.calculators import ActivityLevel, OutOfRange, Sex
from vitalmesh_wellness.claims import Claim, Provenance, Quantity, Recommendation
from vitalmesh_wellness.engine import Goal, Profile, build_recommendation
from vitalmesh_wellness.guardian import (
    METRIC_ENERGY,
    METRIC_HR_ZONE,
    METRIC_SLEEP,
    Guardian,
)
from vitalmesh_wellness.synthetic import synthetic_user


def profile(**kw: object) -> Profile:
    base: dict[str, object] = {
        "user_id": "u1",
        "sex": Sex.MALE,
        "age_years": 30.0,
        "weight_kg": 80.0,
        "height_cm": 180.0,
        "activity": ActivityLevel.MODERATE,
        "resting_hr_bpm": 60.0,
        "goal": Goal.MAINTAIN,
    }
    base.update(kw)
    return Profile(**base)


def value(rec: Recommendation, metric: str) -> float:
    return next(c.quantity.value for c in rec.claims if c.quantity and c.quantity.metric == metric)


def test_reference_recommendation() -> None:
    rec = build_recommendation(profile(), [8.0] * 7, "r1")
    assert value(rec, METRIC_ENERGY) == pytest.approx(1780 * 1.55, abs=0.1)
    assert value(rec, METRIC_HR_ZONE) == 3
    assert value(rec, METRIC_SLEEP) == 8
    assert all(c.provenance is Provenance.COMPUTED for c in rec.claims)


def test_sleep_debt_switches_to_recovery() -> None:
    rec = build_recommendation(profile(), [6.0] * 7, "r1")  # debt = 14 h
    assert value(rec, METRIC_HR_ZONE) == 2
    assert value(rec, METRIC_SLEEP) == 9
    assert any("sleep debt" in c.text for c in rec.claims)


def test_goal_scales_energy_and_floor_is_applied_and_disclosed() -> None:
    maintain = value(build_recommendation(profile(), [8.0], "r"), METRIC_ENERGY)
    lose = value(build_recommendation(profile(goal=Goal.LOSE), [8.0], "r"), METRIC_ENERGY)
    assert lose == pytest.approx(maintain * 0.85, abs=0.1)
    small = profile(
        sex=Sex.FEMALE,
        weight_kg=40,
        height_cm=150,
        age_years=75,
        goal=Goal.LOSE,
        activity=ActivityLevel.SEDENTARY,
    )
    rec = build_recommendation(small, [8.0], "r")
    assert value(rec, METRIC_ENERGY) == 1200
    assert any("minimum safe floor" in c.text for c in rec.claims)


def test_implausible_input_raises_instead_of_guessing() -> None:
    with pytest.raises(OutOfRange):
        build_recommendation(profile(weight_kg=5.0), [8.0], "r")
    with pytest.raises(OutOfRange):
        build_recommendation(profile(), [30.0], "r")


def test_synthetic_users_are_deterministic_and_distinct() -> None:
    assert synthetic_user(1, 5) == synthetic_user(1, 5)
    assert synthetic_user(1, 5) != synthetic_user(1, 6)
    assert synthetic_user(1, 5) != synthetic_user(2, 5)


@given(st.integers(0, 10_000), st.integers(0, 10_000))
def test_engine_output_is_always_cleared_by_the_independent_guardian(seed: int, i: int) -> None:
    p, sleep = synthetic_user(seed, i)
    rec = build_recommendation(p, sleep, f"r-{seed}-{i}")
    verdict = Guardian().evaluate(rec)
    assert verdict.cleared, verdict.violations


def test_guardian_catches_an_engine_bug() -> None:
    # Simulate a buggy engine: a cardiac user handed zone 5 must be blocked by the Guardian.
    p = profile(conditions=frozenset({"cardiac"}))
    bad = Recommendation(
        recommendation_id="x",
        user=p.constraints(),
        claims=(
            Claim(
                text="zone 5",
                provenance=Provenance.COMPUTED,
                source="buggy",
                quantity=Quantity(metric=METRIC_HR_ZONE, value=5, unit="zone"),
            ),
        ),
    )
    assert not Guardian().evaluate(bad).cleared
