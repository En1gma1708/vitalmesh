from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from vitalmesh.events.v1 import common_pb2, payloads_pb2
from vitalmesh_agents.advice import claim_numbers
from vitalmesh_agents.events import derived_id
from vitalmesh_agents.llmcall import AgentUnavailable
from vitalmesh_agents.synthesizer import (
    NO_ANSWER,
    PartView,
    Synthesizer,
    SynthInput,
    assemble,
    check_summary,
    render_prompt,
)
from vitalmesh_agents.synthesizer_handler import (
    make_synthesizer_handler,
    partial_note,
    recommendation_id,
)
from vitalmesh_common import codec
from vitalmesh_common.envelope import EventMeta, Tier
from vitalmesh_common.run import Specialist
from vitalmesh_common.testing import PlainSerde, RecordingPublisher
from vitalmesh_common.topics import RECOMMENDATIONS_DRAFTED
from vitalmesh_llm import AllProvidersFailed, LLMRequest, LLMResponse
from vitalmesh_tracing.testing import RecordingTracer

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
NUT, MOV = common_pb2.SPECIALIST_NUTRITION, common_pb2.SPECIALIST_MOVEMENT_RECOVERY


def resp(text: str, finish: str = "stop") -> LLMResponse:
    return LLMResponse(
        text=text,
        provider="fake",
        model="fake",
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=1.0,
        finish_reason=finish,
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


def synth(router: FakeRouter, **kw: Any) -> Synthesizer:
    return Synthesizer(router, sleep=lambda _s: None, pause_s=0.0, **kw)


def inp(**kw: Any) -> SynthInput:
    base: dict[str, Any] = {
        "question": "What should I eat and how should I train?",
        "risk": "low",
        "parts": (
            PartView("nutrition", "answered", "Aim for about 2124 kcal a day."),
            PartView("movement_recovery", "answered", "Train in zone 3 and sleep 8 hours."),
        ),
        "facts": ("Aim for about 2124 kcal per day.", "Train in zone 3.", "Aim for 8 hours."),
        "allowed_numbers": frozenset({2124.0, 3.0, 8.0}),
    }
    return SynthInput(**{**base, **kw})


# ---------------------------------------------------------------------------------- checks
def test_check_summary_rejects_empty_long_and_stray_numbers() -> None:
    allowed = frozenset({2124.0, 3.0})
    assert check_summary("Eat about 2124 kcal and train in zone 3.", allowed) == []
    assert check_summary("   ", allowed) == ["the reply is empty"]
    assert any("longer" in p for p in check_summary("word " * 200, allowed))
    (stray,) = check_summary("Eat 2500 kcal.", allowed)
    assert "2500" in stray and "not in the facts" in stray


def test_check_summary_accepts_whole_number_rounding_but_not_a_different_number() -> None:
    assert check_summary("About 2124.3 kcal.", frozenset({2124.0})) == []
    assert check_summary("About 2200 kcal.", frozenset({2124.0}))


def test_assemble_joins_the_checked_notes_without_repeats() -> None:
    parts = (
        PartView("a", "answered", "One."),
        PartView("b", "fallback", "Two."),
        PartView("c", "answered", "One."),
    )
    assert assemble(parts) == "One. Two."
    assert assemble(()) == NO_ANSWER


def test_prompt_declares_question_notes_and_facts_as_data_and_lists_missing() -> None:
    prompt = render_prompt(inp(missing=("condition_medication",)))
    assert "<question>" in prompt and "<notes>" in prompt and "<facts>" in prompt
    assert "MISSING: condition_medication" in prompt


# --------------------------------------------------------------------------------- agent
def test_a_checked_draft_is_returned_as_is() -> None:
    router = FakeRouter(resp("Eat about 2124 kcal and train in zone 3, sleeping 8 hours."))
    result = synth(router).synthesize(inp())
    assert not result.used_fallback and result.model_calls == 1
    assert "2124" in result.text
    assert router.requests[0].purpose == "synthesizer"
    assert "kind language" in router.requests[0].messages[0].content


def test_one_retry_sees_the_problems_and_a_good_second_draft_is_used() -> None:
    router = FakeRouter(resp("Eat 9999 kcal."), resp("Eat about 2124 kcal."))
    result = synth(router).synthesize(inp())
    assert not result.used_fallback and result.model_calls == 2
    assert "9999" in router.requests[1].messages[1].content  # the failed draft and its problem
    assert "not in the facts" in router.requests[1].messages[1].content


def test_two_bad_drafts_fall_back_to_the_specialists_checked_notes() -> None:
    router = FakeRouter(resp("Eat 9999 kcal."), resp("Eat 8888 kcal."))
    result = synth(router).synthesize(inp())
    assert result.used_fallback and result.model_calls == 2
    assert result.text == "Aim for about 2124 kcal a day. Train in zone 3 and sleep 8 hours."
    assert "9999" not in result.text and "8888" not in result.text


def test_a_reply_that_hit_the_token_limit_is_asked_again_with_more_room() -> None:
    router = FakeRouter(resp("", finish="length"), resp("Eat about 2124 kcal."))
    result = synth(router).synthesize(inp())
    assert not result.used_fallback
    assert router.requests[1].max_tokens > router.requests[0].max_tokens


def test_provider_outage_is_raised_not_papered_over() -> None:
    router = FakeRouter(AllProvidersFailed(attempts=["groq: 503"]))
    with pytest.raises(AgentUnavailable):
        synth(router).synthesize(inp())
    assert len(router.requests) == 2  # two bounded attempts


def test_a_revision_prompt_carries_the_checker_problems_and_the_previous_text() -> None:
    router = FakeRouter(resp("Eat about 2124 kcal."))
    synth(router).synthesize(
        inp(problems=("the reply ignores the cardiac limit",), previous_text="Run hard.")
    )
    user = router.requests[0].messages[1].content
    assert "ignores the cardiac limit" in user and "Run hard." in user


def test_synthesis_is_traced_with_a_guardrail_and_a_fallback_score() -> None:
    tracer = RecordingTracer()
    synth(FakeRouter(resp("Eat about 2124 kcal.")), tracer=tracer).synthesize(inp())
    assert any(r.name == "synthesize" and r.kind == "run" for r in tracer.records)
    assert any(r.name == "verify-summary" and r.kind == "guardrail" for r in tracer.records)
    assert ("synthesis-fallback", False) in tracer.scores


# -------------------------------------------------------------------------------- handler
def bundle_event(
    *, partial: bool = False, parts: list[payloads_pb2.AdviceCompleted] | None = None
) -> tuple[EventMeta, Any]:
    meta = EventMeta(
        event_id="bundle-1",
        correlation_id="run-1",
        user_id="u-1",
        household_id="h-1",
        type="advice.bundle.ready",
        occurred_at=NOW,
        trace_id="t-1",
        tier=Tier.T3,
        deadline_at=NOW + timedelta(seconds=120),
    )
    b = payloads_pb2.AdviceBundleReady(
        question="What should I eat and how should I train?",
        risk=common_pb2.RISK_LOW,
        partial=partial,
        reason="deadline" if partial else "complete",
    )
    b.parts.extend(parts or [advice(NUT, "Aim for about 2124 kcal a day.", 2124.0, "kcal")])
    if partial:
        b.missing.append(MOV)
    return meta, b


def advice(
    kind: int, text: str, value: float, unit: str, status: int = payloads_pb2.ADVICE_ANSWERED
) -> payloads_pb2.AdviceCompleted:
    a = payloads_pb2.AdviceCompleted(specialist=kind, status=status, text=text)
    c = a.claims.add(
        text=text, provenance=common_pb2.PROVENANCE_COMPUTED, source="engine@1", metric="m"
    )
    c.quantity.value, c.quantity.unit = value, unit
    return a


def run_handler(
    router: FakeRouter, event: tuple[EventMeta, Any]
) -> tuple[RecordingPublisher, EventMeta, Any]:
    pub = RecordingPublisher()
    make_synthesizer_handler(synth(router), PlainSerde(), pub, clock=lambda: NOW)(*event)
    meta, draft = codec.decode(pub.sent[0].value)
    return pub, meta, draft


def test_a_bundle_becomes_a_draft_that_keeps_every_claim_and_provenance() -> None:
    router = FakeRouter(resp("You can aim for about 2124 kcal a day."))
    pub, meta, draft = run_handler(router, bundle_event())
    assert [p.topic for p in pub.sent] == [RECOMMENDATIONS_DRAFTED]
    assert pub.log == ["publish", "flush"]
    assert meta.type == "recommendation.drafted" and meta.correlation_id == "run-1"
    assert meta.event_id == derived_id("bundle-1", "recommendation.drafted.r0")
    assert meta.deadline_at == NOW + timedelta(seconds=120)
    assert draft.recommendation_id == recommendation_id("run-1")
    assert draft.revision == 0 and not draft.partial and not draft.used_fallback
    assert [c.provenance for c in draft.claims] == [common_pb2.PROVENANCE_COMPUTED]
    assert draft.claims[0].quantity.value == 2124.0
    assert draft.text.startswith("You can aim for")


def test_a_partial_bundle_gets_a_disclosure_added_by_code() -> None:
    router = FakeRouter(resp("You can aim for about 2124 kcal a day."))
    _, _, draft = run_handler(router, bundle_event(partial=True))
    assert draft.partial and list(draft.missing) == [MOV]
    assert draft.text.endswith(partial_note([Specialist.MOVEMENT_RECOVERY]))
    assert "movement recovery" in draft.text


def test_a_failed_part_contributes_nothing_but_its_absence_is_reported() -> None:
    failed = payloads_pb2.AdviceCompleted(
        specialist=MOV, status=payloads_pb2.ADVICE_FAILED, failure_code="llm.unavailable"
    )
    event = bundle_event(
        partial=True, parts=[advice(NUT, "About 2124 kcal.", 2124.0, "kcal"), failed]
    )
    router = FakeRouter(resp("You can aim for about 2124 kcal a day."))
    _, _, draft = run_handler(router, event)
    assert len(draft.claims) == 1
    assert "llm.unavailable" not in draft.text


def test_model_outage_degrades_to_the_checked_notes_and_still_publishes() -> None:
    router = FakeRouter(AllProvidersFailed(attempts=["groq: 503"]))
    _, _, draft = run_handler(router, bundle_event())
    assert draft.used_fallback
    assert draft.text == "Aim for about 2124 kcal a day."


def test_when_no_part_is_usable_the_draft_has_no_claims_for_the_guardian_to_block() -> None:
    failed = payloads_pb2.AdviceCompleted(
        specialist=NUT, status=payloads_pb2.ADVICE_FAILED, failure_code="profile.missing"
    )
    router = FakeRouter(resp("should never be asked"))
    _, _, draft = run_handler(router, bundle_event(partial=True, parts=[failed]))
    assert router.requests == []
    assert draft.used_fallback and len(draft.claims) == 0
    assert NO_ANSWER in draft.text


def review_event(verdict: int, revision: int) -> tuple[EventMeta, Any]:
    meta = EventMeta(
        event_id="review-1",
        correlation_id="run-1",
        user_id="u-1",
        household_id="h-1",
        type="review.completed",
        occurred_at=NOW,
        tier=Tier.T3,
        deadline_at=NOW + timedelta(seconds=120),
    )
    review = payloads_pb2.ReviewCompleted(recommendation_id="rec", verdict=verdict)
    review.problems.append("the reply ignores the cardiac limit")
    review.draft.CopyFrom(
        payloads_pb2.RecommendationDrafted(
            recommendation_id=recommendation_id("run-1"),
            question="How should I train?",
            text="Run hard for 40 minutes.",
            risk=common_pb2.RISK_MEDIUM,
            revision=revision,
        )
    )
    review.draft.claims.add(
        text="Train in zone 3 (126-139 bpm).",
        provenance=common_pb2.PROVENANCE_COMPUTED,
        source="engine@1",
    ).quantity.value = 3.0
    review.draft.claims[0].quantity.unit = "zone"
    return meta, review


def test_one_revise_verdict_produces_exactly_one_rewrite() -> None:
    router = FakeRouter(resp("Keep it easy in zone 3."))
    _, meta, draft = run_handler(router, review_event(payloads_pb2.REVIEW_REVISE, 0))
    assert draft.revision == 1 and draft.text == "Keep it easy in zone 3."
    assert meta.event_id == derived_id("review-1", "recommendation.drafted.r1")
    assert "ignores the cardiac limit" in router.requests[0].messages[1].content


def test_a_second_revise_or_other_verdict_is_not_rewritten() -> None:
    for verdict, revision in (
        (payloads_pb2.REVIEW_REVISE, 1),
        (payloads_pb2.REVIEW_APPROVED, 0),
        (payloads_pb2.REVIEW_REJECTED, 0),
    ):
        router = FakeRouter(resp("unused"))
        pub = RecordingPublisher()
        make_synthesizer_handler(synth(router), PlainSerde(), pub)(*review_event(verdict, revision))
        assert pub.sent == [] and router.requests == []


def test_claim_numbers_feed_the_allowed_set() -> None:
    _, draft = bundle_event()[1], None
    assert 2124.0 in claim_numbers(list(bundle_event()[1].parts[0].claims))
    assert draft is None


# --------------------------------------------------------- live-run fixes (grounded, bounded)
def test_the_fallback_prefers_grounded_notes_and_drops_ungrounded_rambling() -> None:
    parts = (
        PartView("condition_medication", "answered", "Eat well and exercise.", grounded=False),
        PartView("nutrition", "answered", "Aim for about 2124 kcal a day."),
    )
    assert assemble(parts) == "Aim for about 2124 kcal a day."
    only_ungrounded = (PartView("condition_medication", "answered", "Nothing declared.", False),)
    assert assemble(only_ungrounded) == "Nothing declared."


def test_the_fallback_stays_inside_the_guardian_word_limit() -> None:
    long_note = " ".join(f"Sentence number {i} is here." for i in range(60))  # 300 words
    out = assemble((PartView("nutrition", "answered", long_note),))
    assert len(out.split()) <= 150 and out.endswith(".")
    two = (
        PartView("nutrition", "answered", "word " * 100),
        PartView("movement_recovery", "answered", "other " * 100),
    )
    assert "other" not in assemble(two)  # a whole part is dropped rather than cut mid-way


def test_declined_notes_come_after_grounded_ones() -> None:
    parts = (
        PartView("condition_medication", "declined", "Please ask a doctor about medication."),
        PartView("nutrition", "answered", "Aim for about 2124 kcal a day."),
    )
    assert assemble(parts) == "Aim for about 2124 kcal a day. Please ask a doctor about medication."


def test_an_ungrounded_part_is_not_shown_to_the_model_when_grounded_notes_exist() -> None:
    rambling = payloads_pb2.AdviceCompleted(
        specialist=common_pb2.SPECIALIST_CONDITION_MEDICATION,
        status=payloads_pb2.ADVICE_ANSWERED,
        text="Drink water and stretch daily.",
    )
    event = bundle_event(
        parts=[advice(NUT, "Aim for about 2124 kcal a day.", 2124.0, "kcal"), rambling]
    )
    router = FakeRouter(resp("You can aim for about 2124 kcal a day."))
    run_handler(router, event)
    assert "Drink water" not in router.requests[0].messages[1].content


# ------------------------------------------------- a cut-off reply is never an answer (found live)
def test_a_non_empty_reply_cut_off_at_the_token_limit_is_asked_again_with_more_room() -> None:
    router = FakeRouter(
        resp("Continue the balanced meals; each day provides a good mix of protein,", "length"),
        resp("Eat about 2124 kcal."),
    )
    result = synth(router).synthesize(inp())
    assert not result.used_fallback and result.text == "Eat about 2124 kcal."
    assert router.requests[1].max_tokens > router.requests[0].max_tokens


def test_a_reply_cut_off_twice_is_not_used_it_falls_back_to_the_checked_notes() -> None:
    cut = resp("Eat about 2124 kcal and then", "length")
    result = synth(FakeRouter(cut)).synthesize(inp())
    assert result.used_fallback and "cut off" in result.reason
    assert "and then" not in result.text
