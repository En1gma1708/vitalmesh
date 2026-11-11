"""Guardian rules version 5: weight targets, loss pace and extreme BMI (D108). Exact boundaries."""

from __future__ import annotations

import pytest
from vitalmesh_wellness import guardian as guardian_module
from vitalmesh_wellness.claims import Claim, Provenance, Quantity, Recommendation, UserConstraints
from vitalmesh_wellness.guardian import (
    EXPECTED_UNIT,
    METRIC_BMI,
    METRIC_LOSS_RATE,
    METRIC_LOSS_RATE_PCT,
    METRIC_MILESTONE_BMI,
    METRIC_WEIGHT_MILESTONE,
    Guardian,
    rule_bmi_extreme_review,
    rule_weight_loss_rate_safe,
    rule_weight_target_safe,
)


def claim(metric: str, value: float) -> Claim:
    return Claim(
        text=f"{metric} {value:g}",
        provenance=Provenance.COMPUTED,
        source="test",
        quantity=Quantity(metric=metric, value=value, unit=EXPECTED_UNIT[metric]),
    )


def rec(*claims: Claim) -> Recommendation:
    user = UserConstraints(user_id="u", sex="female", age_years=35)
    return Recommendation(recommendation_id="r", user=user, claims=tuple(claims))


def test_the_new_rules_are_part_of_the_default_set() -> None:
    for rule in (rule_weight_target_safe, rule_weight_loss_rate_safe, rule_bmi_extreme_review):
        assert rule in guardian_module.DEFAULT_RULES


@pytest.mark.parametrize(
    ("bmi", "bad"), [(18.4, True), (18.49, True), (18.5, False), (22.0, False)]
)
def test_a_target_may_not_give_an_underweight_bmi(bmi: float, bad: bool) -> None:
    r = rec(claim(METRIC_WEIGHT_MILESTONE, 60), claim(METRIC_MILESTONE_BMI, bmi))
    assert bool(rule_weight_target_safe(r)) is bad


def test_a_target_without_its_bmi_cannot_be_checked_and_blocks() -> None:
    v = rule_weight_target_safe(rec(claim(METRIC_WEIGHT_MILESTONE, 60)))
    assert [x.rule for x in v] == ["weight_target_safe"]
    assert "BMI" in v[0].message


def test_no_target_means_nothing_to_check() -> None:
    assert rule_weight_target_safe(rec(claim(METRIC_BMI, 22))) == []


@pytest.mark.parametrize(("rate", "bad"), [(0.5, False), (1.0, False), (1.01, True), (2.0, True)])
def test_loss_rate_in_kg_is_capped_at_one_kg_a_week(rate: float, bad: bool) -> None:
    assert bool(rule_weight_loss_rate_safe(rec(claim(METRIC_LOSS_RATE, rate)))) is bad


@pytest.mark.parametrize(("pct", "bad"), [(0.5, False), (1.0, False), (1.01, True), (3.0, True)])
def test_loss_rate_in_percent_is_capped_at_one_percent_a_week(pct: float, bad: bool) -> None:
    assert bool(rule_weight_loss_rate_safe(rec(claim(METRIC_LOSS_RATE_PCT, pct)))) is bad


def test_each_rate_violation_is_reported_separately() -> None:
    v = rule_weight_loss_rate_safe(rec(claim(METRIC_LOSS_RATE, 2), claim(METRIC_LOSS_RATE_PCT, 3)))
    assert len(v) == 2 and {x.rule for x in v} == {"weight_loss_rate_safe"}


@pytest.mark.parametrize(
    ("bmi", "bad"), [(15.9, True), (16.0, False), (22.0, False), (39.9, False), (40.0, True)]
)
def test_a_plan_at_an_extreme_bmi_needs_a_clinician(bmi: float, bad: bool) -> None:
    r = rec(claim(METRIC_BMI, bmi), claim(METRIC_WEIGHT_MILESTONE, 70))
    assert bool(rule_bmi_extreme_review(r)) is bad
    r2 = rec(claim(METRIC_BMI, bmi), claim(METRIC_LOSS_RATE, 0.5))
    assert bool(rule_bmi_extreme_review(r2)) is bad


def test_an_extreme_bmi_may_be_stated_without_a_plan() -> None:
    assert rule_bmi_extreme_review(rec(claim(METRIC_BMI, 41))) == []
    assert rule_bmi_extreme_review(rec(claim(METRIC_BMI, 15))) == []


@pytest.mark.parametrize(
    "metric",
    [
        "bmi",
        "body_weight",
        "healthy_weight_low",
        "healthy_weight_high",
        "weight_milestone",
        "weight_milestone_bmi",
        "weight_loss_rate",
        "weight_loss_rate_pct",
    ],
)
def test_units_are_enforced_for_every_weight_metric(metric: str) -> None:
    from vitalmesh_wellness.guardian import rule_units_match

    wrong = Claim(
        text="x",
        provenance=Provenance.COMPUTED,
        source="t",
        quantity=Quantity(metric=metric, value=22, unit="wrong"),
    )
    assert [v.rule for v in rule_units_match(rec(wrong))] == ["units_match"]
    assert rule_units_match(rec(claim(metric, 22))) == []


def test_the_guardian_blocks_an_underweight_target_end_to_end() -> None:
    verdict = Guardian().evaluate(
        rec(claim(METRIC_WEIGHT_MILESTONE, 45), claim(METRIC_MILESTONE_BMI, 15.2))
    )
    assert not verdict.cleared
    assert "weight_target_safe" in {v.rule for v in verdict.violations}
    assert verdict.rules_version == "5"


def test_the_guardian_clears_a_sensible_weight_review() -> None:
    verdict = Guardian().evaluate(
        rec(
            claim(METRIC_BMI, 27.1),
            claim(METRIC_WEIGHT_MILESTONE, 82.5),
            claim(METRIC_MILESTONE_BMI, 25.7),
            claim(METRIC_LOSS_RATE, 0.5),
            claim(METRIC_LOSS_RATE_PCT, 0.6),
        )
    )
    assert verdict.cleared, verdict.violations


def test_a_violation_points_at_the_claim_it_is_about() -> None:
    target = claim(METRIC_WEIGHT_MILESTONE, 60)
    v = rule_weight_target_safe(rec(target))
    assert v[0].claim_text == target.text
    low = claim(METRIC_MILESTONE_BMI, 17.0)
    v = rule_weight_target_safe(rec(claim(METRIC_WEIGHT_MILESTONE, 50), low))
    assert [x.claim_text for x in v] == [low.text]


def test_with_several_targets_and_no_bmi_the_first_target_is_the_one_named() -> None:
    first, second = claim(METRIC_WEIGHT_MILESTONE, 80), claim(METRIC_WEIGHT_MILESTONE, 75)
    v = rule_weight_target_safe(rec(first, second))
    assert [x.claim_text for x in v] == [first.text]
