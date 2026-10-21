"""Wire contract of the T3 multi-agent run (D68): every payload round-trips and a payload
cannot be sent under another event's type."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from vitalmesh.events.v1 import common_pb2, payloads_pb2
from vitalmesh_common import codec
from vitalmesh_common.envelope import EventMeta, Tier
from vitalmesh_common.failure import EnvelopeError

T0 = datetime(2026, 10, 22, 8, 0, tzinfo=UTC)


def meta(type_: str, **extra: object) -> EventMeta:
    return EventMeta(
        correlation_id="run-1",
        user_id="u-1",
        household_id="h-1",
        type=type_,
        occurred_at=T0,
        **extra,
    )


def claim() -> payloads_pb2.RecommendationClaim:
    c = payloads_pb2.RecommendationClaim(
        text="Aim for about 2124 kcal per day.",
        provenance=common_pb2.PROVENANCE_COMPUTED,
        source="calculators@1",
        metric="daily_energy_target",
    )
    c.quantity.value = 2124.0
    c.quantity.unit = "kcal"
    return c


def citation() -> payloads_pb2.Citation:
    return payloads_pb2.Citation(
        document="Physical Activity Guidelines",
        locator="p. 34",
        licence="US federal government work (17 U.S.C. 105)",
        source_url="https://example.org/pag.pdf",
        quote="Adults need at least 150 minutes of moderate-intensity activity a week.",
    )


def draft(revision: int = 0) -> payloads_pb2.RecommendationDrafted:
    d = payloads_pb2.RecommendationDrafted(
        recommendation_id="rec-1",
        question="How should I train and eat this week?",
        text="Eat about 2124 kcal and aim for 150 minutes of activity.",
        risk=common_pb2.RISK_MEDIUM,
        partial=True,
        revision=revision,
        used_fallback=False,
    )
    d.claims.append(claim())
    d.citations.append(citation())
    d.missing.append(common_pb2.SPECIALIST_CONDITION_MEDICATION)
    return d


def test_question_and_plan_round_trip_with_tier_and_deadline() -> None:
    deadline = T0 + timedelta(seconds=30)
    plan = payloads_pb2.RunPlanned(
        question="q",
        risk=common_pb2.RISK_HIGH,
        max_model_calls=5,
        degraded=True,
    )
    plan.specialists.extend(
        [common_pb2.SPECIALIST_NUTRITION, common_pb2.SPECIALIST_MOVEMENT_RECOVERY]
    )
    plan.rule_ids.append("risk.cardiac_condition")
    got_meta, got = codec.decode(
        codec.encode(meta("run.planned", tier=Tier.T3, deadline_at=deadline), plan)
    )
    assert got_meta.tier is Tier.T3
    assert got_meta.deadline_at == deadline
    assert list(got.specialists) == [
        common_pb2.SPECIALIST_NUTRITION,
        common_pb2.SPECIALIST_MOVEMENT_RECOVERY,
    ]
    assert got.risk == common_pb2.RISK_HIGH
    assert got.degraded
    assert list(got.rule_ids) == ["risk.cardiac_condition"]

    asked = payloads_pb2.QuestionAsked(question="What should I eat?", session_id="s-1")
    _, got_q = codec.decode(codec.encode(meta("question.asked"), asked))
    assert got_q.question == "What should I eat?"


def test_advice_completed_keeps_claims_citations_and_failure_code() -> None:
    advice = payloads_pb2.AdviceCompleted(
        specialist=common_pb2.SPECIALIST_NUTRITION,
        status=payloads_pb2.ADVICE_ANSWERED,
        text="About 2124 kcal.",
        model_calls=2,
        tool_calls=1,
    )
    advice.claims.append(claim())
    advice.citations.append(citation())
    _, got = codec.decode(codec.encode(meta("advice.completed"), advice))
    assert got.specialist == common_pb2.SPECIALIST_NUTRITION
    assert got.claims[0].quantity.unit == "kcal"
    assert got.claims[0].provenance == common_pb2.PROVENANCE_COMPUTED
    assert got.citations[0].locator == "p. 34"

    failed = payloads_pb2.AdviceCompleted(
        specialist=common_pb2.SPECIALIST_MOVEMENT_RECOVERY,
        status=payloads_pb2.ADVICE_FAILED,
        failure_code="llm.unavailable",
    )
    _, got_f = codec.decode(codec.encode(meta("advice.completed"), failed))
    assert got_f.status == payloads_pb2.ADVICE_FAILED
    assert got_f.failure_code == "llm.unavailable"


def test_bundle_carries_parts_missing_and_partial_flag() -> None:
    bundle = payloads_pb2.AdviceBundleReady(
        question="q", risk=common_pb2.RISK_LOW, partial=True, reason="deadline"
    )
    bundle.parts.add(
        specialist=common_pb2.SPECIALIST_NUTRITION, status=payloads_pb2.ADVICE_ANSWERED
    )
    bundle.missing.append(common_pb2.SPECIALIST_MOVEMENT_RECOVERY)
    _, got = codec.decode(codec.encode(meta("advice.bundle.ready"), bundle))
    assert got.partial
    assert got.reason == "deadline"
    assert list(got.missing) == [common_pb2.SPECIALIST_MOVEMENT_RECOVERY]
    assert got.parts[0].specialist == common_pb2.SPECIALIST_NUTRITION


def test_draft_cleared_and_review_payloads_nest_the_draft() -> None:
    _, got_draft = codec.decode(codec.encode(meta("recommendation.drafted"), draft(revision=1)))
    assert got_draft.revision == 1
    assert got_draft.partial
    assert got_draft.claims[0].metric == "daily_energy_target"

    cleared = payloads_pb2.SafetyCleared(rules_version="3")
    cleared.draft.CopyFrom(draft())
    _, got_cleared = codec.decode(codec.encode(meta("safety.cleared"), cleared))
    assert got_cleared.draft.recommendation_id == "rec-1"
    assert got_cleared.rules_version == "3"

    review = payloads_pb2.ReviewCompleted(
        recommendation_id="rec-1", verdict=payloads_pb2.REVIEW_REVISE, rules_version="3"
    )
    review.problems.append("the plan quotes 3000 kcal, which no claim supports")
    review.draft.CopyFrom(draft())
    _, got_review = codec.decode(codec.encode(meta("review.completed"), review))
    assert got_review.verdict == payloads_pb2.REVIEW_REVISE
    assert got_review.problems[0].startswith("the plan quotes")
    assert got_review.draft.text.startswith("Eat about")

    request = payloads_pb2.ReviewRequested(
        recommendation_id="rec-1", briefing="Why flagged: high risk.", rules_version="3"
    )
    request.reasons.append("risk.high")
    request.draft.CopyFrom(draft())
    _, got_request = codec.decode(codec.encode(meta("review.requested"), request))
    assert list(got_request.reasons) == ["risk.high"]

    decided = payloads_pb2.ReviewDecided(
        recommendation_id="rec-1",
        decision=payloads_pb2.DECISION_APPROVE,
        reviewer_id="clinician-1",
        note="ok",
    )
    _, got_decided = codec.decode(codec.encode(meta("review.decided"), decided))
    assert got_decided.decision == payloads_pb2.DECISION_APPROVE
    assert got_decided.reviewer_id == "clinician-1"


def test_unspecified_enums_are_the_wire_default_so_consumers_can_reject_them() -> None:
    plan = payloads_pb2.RunPlanned(question="q")
    _, got = codec.decode(codec.encode(meta("run.planned"), plan))
    assert got.risk == common_pb2.RISK_UNSPECIFIED
    assert len(got.specialists) == 0


@pytest.mark.parametrize(
    ("type_", "payload"),
    [
        ("run.planned", payloads_pb2.QuestionAsked(question="q")),
        ("question.asked", payloads_pb2.RunPlanned(question="q")),
        ("advice.completed", payloads_pb2.AdviceBundleReady()),
        ("safety.cleared", draft()),
        ("recommendation.drafted", payloads_pb2.SafetyCleared()),
        ("review.decided", payloads_pb2.ReviewRequested()),
    ],
)
def test_type_must_match_payload(type_: str, payload: object) -> None:
    with pytest.raises(EnvelopeError):
        codec.encode(meta(type_), payload)


def test_every_run_event_type_is_registered() -> None:
    for type_ in (
        "question.asked",
        "run.planned",
        "advice.completed",
        "advice.bundle.ready",
        "recommendation.drafted",
        "safety.cleared",
        "review.completed",
        "review.requested",
        "review.decided",
    ):
        assert type_ in codec.PAYLOAD_FIELDS
