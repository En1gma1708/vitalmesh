from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from vitalmesh.events.v1 import common_pb2, payloads_pb2
from vitalmesh_agents.briefer import Briefer, CaseInput, check_briefing, fallback_briefing
from vitalmesh_agents.events import derived_id
from vitalmesh_agents.release import ENGINE_VERSION, ReleaseGate
from vitalmesh_agents.release_handler import make_release_handler
from vitalmesh_common import codec
from vitalmesh_common.envelope import EventMeta, Tier
from vitalmesh_common.failure import EnvelopeError
from vitalmesh_common.testing import PlainSerde, RecordingPublisher
from vitalmesh_common.topics import RECOMMENDATIONS_RELEASED, REVIEWS_REQUESTED, SAFETY_BLOCKED
from vitalmesh_llm import AllProvidersFailed, LLMRequest, LLMResponse
from vitalmesh_stores import InMemoryProfileStore, ProfileRecord
from vitalmesh_wellness.engine import Profile

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
RULES = "3+t3-1"
LOW, MEDIUM, HIGH = common_pb2.RISK_LOW, common_pb2.RISK_MEDIUM, common_pb2.RISK_HIGH


def resp(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        provider="fake",
        model="fake",
        prompt_tokens=1,
        completion_tokens=1,
        latency_ms=1.0,
        finish_reason="stop",
    )


class FakeRouter:
    def __init__(self, *replies: LLMResponse | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[LLMRequest] = []

    def complete(self, req: LLMRequest, max_wait_s: float = 0.0) -> LLMResponse:
        self.requests.append(req)
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, Exception):
            raise r
        return r


def profiles(with_user: bool = True) -> InMemoryProfileStore:
    store = InMemoryProfileStore()
    if with_user:
        store.put(
            ProfileRecord(
                profile=Profile.model_validate(
                    {
                        "user_id": "u-1",
                        "sex": "female",
                        "age_years": 30,
                        "weight_kg": 65,
                        "height_cm": 165,
                        "activity": "moderate",
                        "resting_hr_bpm": 60,
                    }
                )
            )
        )
    return store


def gate(
    router: FakeRouter | None = None, with_user: bool = True
) -> tuple[ReleaseGate, RecordingPublisher]:
    pub = RecordingPublisher()
    briefer = Briefer(
        router or FakeRouter(resp("The user asked about calories; the reply gives 2124 kcal.")),
        sleep=lambda _s: None,
        pause_s=0.0,
    )
    g = ReleaseGate(
        profiles(with_user), briefer, PlainSerde(), pub, InMemorySaver(), clock=lambda: NOW
    )
    return g, pub


def draft(
    risk: int, text: str = "Aim for about 2124 kcal a day.", revision: int = 0
) -> payloads_pb2.RecommendationDrafted:
    d = payloads_pb2.RecommendationDrafted(
        recommendation_id="rec-1",
        question="How many calories?",
        text=text,
        risk=risk,
        revision=revision,
    )
    c = d.claims.add(
        text="Aim for about 2124 kcal a day.",
        provenance=common_pb2.PROVENANCE_COMPUTED,
        source="engine@1",
        metric="daily_energy_target",
    )
    c.quantity.value, c.quantity.unit = 2124.0, "kcal"
    return d


def meta(type_: str, event_id: str = "evt-1") -> EventMeta:
    return EventMeta(
        event_id=event_id,
        correlation_id="run-1",
        user_id="u-1",
        household_id="h-1",
        type=type_,
        occurred_at=NOW,
        trace_id="t-1",
        tier=Tier.T3,
        deadline_at=NOW + timedelta(seconds=120),
    )


def cleared(d: payloads_pb2.RecommendationDrafted) -> tuple[EventMeta, Any]:
    msg = payloads_pb2.SafetyCleared(rules_version=RULES)
    msg.draft.CopyFrom(d)
    return meta("safety.cleared"), msg


def reviewed(
    d: payloads_pb2.RecommendationDrafted, verdict: int, *problems: str
) -> tuple[EventMeta, Any]:
    msg = payloads_pb2.ReviewCompleted(
        recommendation_id=d.recommendation_id, verdict=verdict, rules_version=RULES
    )
    msg.problems.extend(problems)
    msg.draft.CopyFrom(d)
    return meta("review.completed", "rev-1"), msg


def decided(decision: int, reviewer: str = "clinician-1") -> tuple[EventMeta, Any]:
    return meta("review.decided", "dec-1"), payloads_pb2.ReviewDecided(
        recommendation_id="rec-1", decision=decision, reviewer_id=reviewer
    )


def topics(pub: RecordingPublisher) -> list[str]:
    return [p.topic for p in pub.sent]


def sent(pub: RecordingPublisher, i: int = -1) -> tuple[EventMeta, Any]:
    return codec.decode(pub.sent[i].value)


# ---------------------------------------------------------------------- auto release
def test_a_cleared_low_risk_draft_is_released_with_its_text_claims_and_rules_version() -> None:
    g, pub = gate()
    make_release_handler(g)(*cleared(draft(LOW)))
    assert topics(pub) == [RECOMMENDATIONS_RELEASED] and pub.log == ["publish", "flush"]
    m, released = sent(pub)
    assert m.type == "recommendation.released" and m.tier is Tier.T3
    assert m.event_id == derived_id("rec-1", "released") and m.rules_version == RULES
    assert m.correlation_id == "run-1" and m.trace_id == "t-1"
    assert released.summary == "Aim for about 2124 kcal a day."
    assert released.engine_version == ENGINE_VERSION and released.rules_version == RULES
    assert released.claims[0].quantity.value == 2124.0 and released.question


def test_redelivery_does_not_release_twice() -> None:
    g, pub = gate()
    h = make_release_handler(g)
    h(*cleared(draft(LOW)))
    h(*cleared(draft(LOW)))
    assert topics(pub) == [RECOMMENDATIONS_RELEASED]


def test_medium_and_high_drafts_are_not_released_by_the_cleared_event() -> None:
    for risk in (MEDIUM, HIGH):
        g, pub = gate()
        make_release_handler(g)(*cleared(draft(risk)))
        assert pub.sent == []  # the Checker's turn


def test_the_final_guardian_blocks_what_an_earlier_stage_let_through() -> None:
    g, pub = gate()
    tampered = draft(LOW, text="Aim for about 9999 kcal a day.")  # a number no claim supports
    make_release_handler(g)(*cleared(tampered))
    assert topics(pub) == [SAFETY_BLOCKED]
    m, blocked = sent(pub)
    assert m.event_id == derived_id("rec-1", "release.blocked")
    assert any(v.rule == "numbers_grounded" for v in blocked.violations)


def test_an_unknown_user_is_blocked_at_release() -> None:
    g, pub = gate(with_user=False)
    make_release_handler(g)(*cleared(draft(LOW)))
    assert topics(pub) == [SAFETY_BLOCKED]


# ------------------------------------------------------------- checker verdict routing
def test_a_medium_risk_draft_the_checker_approved_is_released() -> None:
    g, pub = gate()
    make_release_handler(g)(*reviewed(draft(MEDIUM), payloads_pb2.REVIEW_APPROVED))
    assert topics(pub) == [RECOMMENDATIONS_RELEASED]


def test_a_high_risk_draft_is_never_released_without_a_human_even_if_approved() -> None:
    g, pub = gate()
    make_release_handler(g)(*reviewed(draft(HIGH), payloads_pb2.REVIEW_APPROVED))
    assert topics(pub) == [REVIEWS_REQUESTED]
    assert g.pending("rec-1")
    m, request = sent(pub)
    assert m.type == "review.requested" and m.event_id == derived_id("rec-1", "review.requested")
    assert list(request.reasons) == ["risk.high"]
    assert request.briefing and request.draft.recommendation_id == "rec-1"


def test_revise_on_the_first_draft_is_left_to_the_synthesizer() -> None:
    g, pub = gate()
    make_release_handler(g)(*reviewed(draft(MEDIUM, revision=0), payloads_pb2.REVIEW_REVISE, "x"))
    assert pub.sent == [] and not g.pending("rec-1")


def test_revise_after_the_one_rewrite_goes_to_a_human() -> None:
    g, pub = gate()
    make_release_handler(g)(*reviewed(draft(MEDIUM, revision=1), payloads_pb2.REVIEW_REVISE, "x"))
    assert topics(pub) == [REVIEWS_REQUESTED]
    assert list(sent(pub)[1].reasons) == ["checker.revision_not_enough"]


def test_a_rejection_goes_to_a_human_with_the_checkers_problems_in_the_case() -> None:
    router = FakeRouter(resp("The reply was rejected by the reviewer."))
    g, pub = gate(router)
    make_release_handler(g)(
        *reviewed(draft(MEDIUM), payloads_pb2.REVIEW_REJECTED, "unsupported claim")
    )
    assert list(sent(pub)[1].reasons) == ["checker.rejected"]
    assert "unsupported claim" in router.requests[0].messages[1].content


def test_an_unspecified_verdict_is_an_invalid_event() -> None:
    g, _ = gate()
    with pytest.raises(EnvelopeError):
        make_release_handler(g)(*reviewed(draft(MEDIUM), payloads_pb2.REVIEW_VERDICT_UNSPECIFIED))


# ------------------------------------------------------------------------- human review
def waiting(
    risk: int = HIGH, d: payloads_pb2.RecommendationDrafted | None = None
) -> tuple[ReleaseGate, RecordingPublisher]:
    g, pub = gate()
    make_release_handler(g)(*reviewed(d or draft(risk), payloads_pb2.REVIEW_REJECTED, "p"))
    return g, pub


def test_a_human_approval_resumes_the_run_and_releases() -> None:
    g, pub = waiting()
    make_release_handler(g)(*decided(payloads_pb2.DECISION_APPROVE))
    assert topics(pub) == [REVIEWS_REQUESTED, RECOMMENDATIONS_RELEASED]
    assert not g.pending("rec-1")
    assert sent(pub)[1].summary.startswith("Aim for")


def test_a_human_rejection_blocks_and_releases_nothing() -> None:
    g, pub = waiting()
    make_release_handler(g)(*decided(payloads_pb2.DECISION_REJECT))
    assert topics(pub) == [REVIEWS_REQUESTED, SAFETY_BLOCKED]
    m, blocked = sent(pub)
    assert m.event_id == derived_id("rec-1", "human.blocked")
    assert blocked.violations[0].rule == "human_review_rejected"


def test_a_human_approval_cannot_override_the_final_guardian() -> None:
    bad = draft(HIGH, text="Aim for about 9999 kcal a day.")
    g, pub = waiting(d=bad)
    make_release_handler(g)(*decided(payloads_pb2.DECISION_APPROVE))
    assert topics(pub) == [REVIEWS_REQUESTED, SAFETY_BLOCKED]


def test_a_second_decision_or_a_decision_for_nothing_is_ignored() -> None:
    g, pub = waiting()
    h = make_release_handler(g)
    h(*decided(payloads_pb2.DECISION_APPROVE))
    h(*decided(payloads_pb2.DECISION_REJECT))  # too late
    assert topics(pub) == [REVIEWS_REQUESTED, RECOMMENDATIONS_RELEASED]
    g2, pub2 = gate()
    make_release_handler(g2)(*decided(payloads_pb2.DECISION_APPROVE))  # nothing is waiting
    assert pub2.sent == []


def test_redelivering_the_escalation_does_not_ask_twice() -> None:
    g, pub = waiting()
    make_release_handler(g)(*reviewed(draft(HIGH), payloads_pb2.REVIEW_REJECTED, "p"))
    assert topics(pub) == [REVIEWS_REQUESTED]


def test_a_pending_review_survives_a_new_gate_on_the_same_checkpoint() -> None:
    saver = InMemorySaver()
    pub = RecordingPublisher()
    briefer = Briefer(FakeRouter(resp("The reply gives 2124 kcal.")), sleep=lambda _s: None)
    first = ReleaseGate(profiles(), briefer, PlainSerde(), pub, saver, clock=lambda: NOW)
    make_release_handler(first)(*reviewed(draft(HIGH), payloads_pb2.REVIEW_REJECTED, "p"))
    second = ReleaseGate(profiles(), briefer, PlainSerde(), pub, saver, clock=lambda: NOW)
    make_release_handler(second)(*decided(payloads_pb2.DECISION_APPROVE))  # another worker
    assert topics(pub) == [REVIEWS_REQUESTED, RECOMMENDATIONS_RELEASED]


@pytest.mark.parametrize(
    ("decision", "reviewer"),
    [(payloads_pb2.DECISION_APPROVE, "  "), (payloads_pb2.REVIEW_DECISION_UNSPECIFIED, "c-1")],
)
def test_a_decision_needs_a_reviewer_and_a_valid_choice(decision: int, reviewer: str) -> None:
    g, _ = waiting()
    with pytest.raises(EnvelopeError):
        make_release_handler(g)(*decided(decision, reviewer))
    assert g.pending("rec-1")  # still waiting


# ---------------------------------------------------------------------------- briefer
CASE = CaseInput(
    question="How many calories?",
    reply="Aim for about 2124 kcal a day.",
    facts=("Aim for about 2124 kcal a day.",),
    allowed_numbers=frozenset({2124.0}),
    reasons=("risk.high",),
    problems=("needs a look",),
    partial=True,
)


def test_briefing_numbers_must_come_from_the_facts() -> None:
    assert check_briefing("The reply gives 2124 kcal.", CASE.allowed_numbers) == []
    assert check_briefing("The reply gives 5000 kcal.", CASE.allowed_numbers)
    assert check_briefing(" ", CASE.allowed_numbers)


def test_fallback_briefing_is_a_plain_template_of_the_same_facts() -> None:
    text = fallback_briefing(CASE)
    assert "How many calories?" in text and "risk.high" in text and "needs a look" in text
    assert "partial" in text


def test_a_briefing_that_fails_twice_or_a_model_outage_uses_the_template() -> None:
    bad = Briefer(FakeRouter(resp("It says 5000 kcal.")), sleep=lambda _s: None).brief(CASE)
    assert bad.used_fallback and "5000" not in bad.text
    down = Briefer(
        FakeRouter(AllProvidersFailed(attempts=["503"])), sleep=lambda _s: None, pause_s=0.0
    ).brief(CASE)
    assert down.used_fallback and down.reason == "model unavailable"


def test_a_good_briefing_is_used_as_written() -> None:
    result = Briefer(FakeRouter(resp("The user asked about calories; the reply says 2124."))).brief(
        CASE
    )
    assert not result.used_fallback and "2124" in result.text
