import math

import pytest
from pydantic import ValidationError
from vitalmesh_wellness.claims import Claim, Provenance, Quantity, Recommendation, UserConstraints
from vitalmesh_wellness.guardian import (
    METRIC_ENERGY,
    METRIC_HR_ZONE,
    METRIC_SLEEP,
    RULES_VERSION,
    Guardian,
    Rule,
    Violation,
)


def user(sex: str = "female", conditions: frozenset[str] = frozenset()) -> UserConstraints:
    return UserConstraints(user_id="u1", sex=sex, age_years=30, conditions=conditions)


def computed(metric: str, value: float, unit: str, text: str = "target") -> Claim:
    return Claim(
        text=text,
        provenance=Provenance.COMPUTED,
        source="calculators@1",
        quantity=Quantity(metric=metric, value=value, unit=unit),
    )


def rec(*claims: Claim, u: UserConstraints | None = None) -> Recommendation:
    return Recommendation(recommendation_id="r1", user=u or user(), claims=claims)


def rules_hit(r: Recommendation) -> set[str]:
    return {v.rule for v in Guardian().evaluate(r).violations}


# ---- claims: provenance must be backed -------------------------------------------------
def test_computed_claim_needs_source_and_quantity() -> None:
    quantity = Quantity(metric="m", value=1, unit="u")
    with pytest.raises(ValidationError):
        Claim(text="x", provenance=Provenance.COMPUTED, quantity=quantity)
    with pytest.raises(ValidationError):
        Claim(text="x", provenance=Provenance.COMPUTED, source="calc")


def test_cited_and_inferred_need_a_nonblank_source() -> None:
    for p in (Provenance.CITED, Provenance.INFERRED):
        with pytest.raises(ValidationError):
            Claim(text="x", provenance=p)
        with pytest.raises(ValidationError):
            Claim(text="x", provenance=p, source="   ")
        assert Claim(text="x", provenance=p, source="who-2020").source == "who-2020"


def test_quantity_must_be_finite() -> None:
    for bad in (math.nan, math.inf):
        with pytest.raises(ValidationError):
            Quantity(metric="m", value=bad, unit="u")


# ---- guardian ---------------------------------------------------------------------------
def test_clean_recommendation_is_cleared() -> None:
    v = Guardian().evaluate(
        rec(computed(METRIC_ENERGY, 2000, "kcal"), computed(METRIC_SLEEP, 8, "h"))
    )
    assert v.cleared
    assert not v.violations
    assert v.rules_version == RULES_VERSION


def test_empty_recommendation_is_blocked() -> None:
    assert "has_claims" in rules_hit(rec())


def test_unknown_provenance_is_blocked() -> None:
    unknown = Claim(text="drink kefir", provenance=Provenance.UNKNOWN)
    assert "no_unknown_provenance" in rules_hit(rec(unknown, computed(METRIC_SLEEP, 8, "h")))


@pytest.mark.parametrize(
    ("sex", "kcal", "blocked"),
    [
        ("female", 1199, True),
        ("female", 1200, False),
        ("female", 6000, False),
        ("female", 6001, True),
        ("male", 1499, True),
        ("male", 1500, False),
    ],
)
def test_energy_bounds(sex: str, kcal: float, blocked: bool) -> None:
    r = rec(computed(METRIC_ENERGY, kcal, "kcal"), u=user(sex))
    assert ("energy_bounds" in rules_hit(r)) is blocked


def test_energy_with_unknown_sex_is_blocked() -> None:
    assert "energy_bounds" in rules_hit(rec(computed(METRIC_ENERGY, 2000, "kcal"), u=user("other")))


def test_wrong_unit_is_blocked_not_converted() -> None:
    assert "units_match" in rules_hit(rec(computed(METRIC_ENERGY, 8368, "kJ")))


@pytest.mark.parametrize("cond", ["cardiac", "uncontrolled_hypertension"])
def test_intensity_cap_for_limiting_conditions(cond: str) -> None:
    u = user(conditions=frozenset({cond}))
    assert "intensity_cap" in rules_hit(rec(computed(METRIC_HR_ZONE, 4, "zone"), u=u))
    assert "intensity_cap" not in rules_hit(rec(computed(METRIC_HR_ZONE, 3, "zone"), u=u))


def test_zone_5_is_fine_without_limiting_condition() -> None:
    assert "intensity_cap" not in rules_hit(rec(computed(METRIC_HR_ZONE, 5, "zone")))


@pytest.mark.parametrize(("hours", "blocked"), [(6.9, True), (7, False), (10, False), (10.1, True)])
def test_sleep_bounds(hours: float, blocked: bool) -> None:
    assert ("sleep_bounds" in rules_hit(rec(computed(METRIC_SLEEP, hours, "h")))) is blocked


def test_a_crashing_rule_fails_closed() -> None:
    def boom(_: Recommendation) -> list[Violation]:
        raise RuntimeError("bug in a rule")

    rules: tuple[Rule, ...] = (boom,)
    v = Guardian(rules).evaluate(rec(computed(METRIC_SLEEP, 8, "h")))
    assert not v.cleared
    assert v.violations[0].rule == "boom"
    assert "RuntimeError" in v.violations[0].message


def test_all_violations_are_reported_not_just_the_first() -> None:
    r = rec(
        Claim(text="mystery", provenance=Provenance.UNKNOWN),
        computed(METRIC_ENERGY, 500, "kcal"),
        computed(METRIC_SLEEP, 3, "h"),
    )
    assert {"no_unknown_provenance", "energy_bounds", "sleep_bounds"} <= rules_hit(r)
