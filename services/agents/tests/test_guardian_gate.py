from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from vitalmesh.events.v1 import common_pb2, payloads_pb2
from vitalmesh_agents.guardian_gate import (
    MAX_WORDS,
    T3_RULES_VERSION,
    draft_to_recommendation,
    evaluate_draft,
)
from vitalmesh_agents.guardian_handler import make_guardian_handler
from vitalmesh_common import codec
from vitalmesh_common.envelope import EventMeta, Tier
from vitalmesh_common.testing import PlainSerde, RecordingPublisher
from vitalmesh_common.topics import DRAFTS_CLEARED, SAFETY_BLOCKED
from vitalmesh_stores import InMemoryProfileStore, ProfileRecord
from vitalmesh_wellness.claims import UserConstraints
from vitalmesh_wellness.engine import Profile
from vitalmesh_wellness.guardian import RULES_VERSION, Guardian, Violation

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
USER = UserConstraints(user_id="u-1", sex="female", age_years=30)
CARDIAC = UserConstraints(
    user_id="u-1", sex="female", age_years=30, conditions=frozenset({"cardiac"})
)


def claim(
    text: str,
    metric: str = "daily_energy_target",
    value: float = 2124.0,
    unit: str = "kcal",
    provenance: int = common_pb2.PROVENANCE_COMPUTED,
    source: str | None = "engine@1",
) -> payloads_pb2.RecommendationClaim:
    c = payloads_pb2.RecommendationClaim(text=text, provenance=provenance)
    if source is not None:
        c.source = source
    if metric:
        c.metric = metric
        c.quantity.value, c.quantity.unit = value, unit
    return c


def draft(
    text: str = "Aim for about 2124 kcal a day.",
    claims: list[payloads_pb2.RecommendationClaim] | None = None,
    **kw: Any,
) -> payloads_pb2.RecommendationDrafted:
    d = payloads_pb2.RecommendationDrafted(
        recommendation_id="rec-1",
        question="q",
        text=text,
        risk=kw.pop("risk", common_pb2.RISK_LOW),
        **kw,
    )
    d.claims.extend(claims if claims is not None else [claim("Aim for about 2124 kcal a day.")])
    return d


def rules_hit(
    d: payloads_pb2.RecommendationDrafted, user: UserConstraints | None = USER
) -> set[str]:
    return {v.rule for v in evaluate_draft(d, user).violations}


def test_a_grounded_draft_is_cleared_and_stamped_with_the_combined_rules_version() -> None:
    verdict = evaluate_draft(draft(), USER)
    assert verdict.cleared and verdict.violations == ()
    assert verdict.rules_version == T3_RULES_VERSION == f"{RULES_VERSION}+t3-1"


def test_the_shared_guardian_still_applies_to_t3_claims() -> None:
    zone5 = claim("Train in zone 5.", "target_hr_zone", 5.0, "zone")
    assert "intensity_cap" in rules_hit(draft("Train in zone 5.", [zone5]), CARDIAC)
    assert "intensity_cap" not in rules_hit(draft("Train in zone 5.", [zone5]), USER)
    kj = claim("About 8000 kJ.", "daily_energy_target", 8000.0, "kJ")
    assert "units_match" in rules_hit(draft("About 8000 kJ.", [kj]))
    low = claim("Eat 800 kcal.", "daily_energy_target", 800.0, "kcal")
    assert "energy_bounds" in rules_hit(draft("Eat 800 kcal.", [low]))


def test_unknown_provenance_is_blocked() -> None:
    unknown = claim("Maybe.", metric="", provenance=common_pb2.PROVENANCE_UNKNOWN, source=None)
    assert "no_unknown_provenance" in rules_hit(draft("Maybe.", [unknown]))


def test_a_draft_with_no_claims_is_blocked() -> None:
    assert "has_claims" in rules_hit(draft("Some words.", []))


def test_a_claim_the_domain_refuses_is_a_violation_not_a_crash() -> None:
    no_prov = payloads_pb2.RecommendationClaim(text="x")  # PROVENANCE_UNSPECIFIED
    assert "claims_valid" in rules_hit(draft("x", [no_prov]))
    computed_without_quantity = claim("x", metric="")
    assert "claims_valid" in rules_hit(draft("x", [computed_without_quantity]))
    with pytest.raises(ValueError):
        draft_to_recommendation(draft("x", [no_prov]), USER)


def test_no_profile_means_nothing_can_be_released() -> None:
    verdict = evaluate_draft(draft(), None)
    assert not verdict.cleared and {v.rule for v in verdict.violations} == {"profile_known"}


def test_a_number_in_the_prose_that_no_claim_supports_is_blocked() -> None:
    assert "numbers_grounded" in rules_hit(draft("Aim for about 3000 kcal a day."))
    assert "numbers_grounded" not in rules_hit(draft("Aim for about 2124 kcal a day."))


def test_empty_and_overlong_text_is_blocked() -> None:
    assert "text_present" in rules_hit(draft("  "))
    assert "text_bounded" in rules_hit(draft("word " * (MAX_WORDS + 5)))


def test_a_cited_claim_needs_its_verified_citation() -> None:
    cited = claim(
        "Adults need 150 minutes a week.",
        metric="",
        provenance=common_pb2.PROVENANCE_CITED,
        source="pag-2018 p. 34",
    )
    without = draft("Adults need 150 minutes a week.", [cited])
    assert "cited_has_citation" in rules_hit(without)
    with_citation = draft("Adults need 150 minutes a week.", [cited])
    with_citation.citations.add(document="pag-2018", locator="p. 34", quote="q", licence="l")
    assert "cited_has_citation" not in rules_hit(with_citation)


@pytest.mark.parametrize(
    "text",
    [
        "You should stop taking your medication.",
        "Increase your dose of insulin a little.",
        "Try to skip the statin on training days.",
    ],
)
def test_telling_the_user_to_act_on_medication_is_blocked(text: str) -> None:
    assert "no_medication_advice" in rules_hit(draft(text))


@pytest.mark.parametrize(
    "text",
    ["You probably have diabetes.", "You have hypertension.", "I diagnose a thyroid problem."],
)
def test_stating_a_diagnosis_is_blocked(text: str) -> None:
    assert "no_diagnosis" in rules_hit(draft(text))


def test_ordinary_wellness_wording_is_not_blocked() -> None:
    ok = "Aim for about 2124 kcal a day and ask your doctor about your medication."
    assert rules_hit(draft(ok)) == set()


def test_a_partial_answer_must_say_it_is_partial() -> None:
    assert "partial_disclosed" in rules_hit(draft(partial=True))
    ok = draft("Aim for about 2124 kcal a day. Note: this answer is partial.", partial=True)
    assert "partial_disclosed" not in rules_hit(ok)


def test_a_draft_without_a_risk_level_is_blocked() -> None:
    d = draft()
    d.risk = common_pb2.RISK_UNSPECIFIED
    assert "risk_set" in rules_hit(d)


def test_a_rule_that_raises_blocks_instead_of_passing() -> None:
    def broken(_: Any) -> list[Violation]:
        raise RuntimeError("boom")

    verdict = evaluate_draft(draft(), USER, extra_rules=(broken,))
    assert not verdict.cleared
    assert any("could not run" in v.message for v in verdict.violations)


def test_a_guardian_with_no_rules_does_not_loosen_the_t3_rules() -> None:
    verdict = evaluate_draft(draft("Aim for about 9999 kcal a day."), USER, Guardian(rules=()))
    assert not verdict.cleared  # the prose rules are the gate's own


# ---------------------------------------------------------------------------- handler
def event(d: payloads_pb2.RecommendationDrafted, user_id: str = "u-1") -> tuple[EventMeta, Any]:
    meta = EventMeta(
        event_id="draft-1",
        correlation_id="run-1",
        user_id=user_id,
        household_id="h-1",
        type="recommendation.drafted",
        occurred_at=NOW,
        tier=Tier.T3,
        trace_id="t-1",
        deadline_at=NOW + timedelta(seconds=120),
    )
    return meta, d


def profiles(conditions: frozenset[str] = frozenset()) -> InMemoryProfileStore:
    store = InMemoryProfileStore()
    profile = Profile.model_validate(
        {
            "user_id": "u-1",
            "sex": "female",
            "age_years": 30,
            "weight_kg": 65,
            "height_cm": 165,
            "activity": "moderate",
            "resting_hr_bpm": 60,
            "conditions": sorted(conditions),
        }
    )
    store.put(ProfileRecord(profile=profile))
    return store


def run(
    d: payloads_pb2.RecommendationDrafted, store: InMemoryProfileStore
) -> tuple[RecordingPublisher, EventMeta, Any]:
    pub = RecordingPublisher()
    make_guardian_handler(store, PlainSerde(), pub, clock=lambda: NOW)(*event(d))
    meta, payload = codec.decode(pub.sent[0].value)
    return pub, meta, payload


def test_a_cleared_draft_goes_to_drafts_cleared_and_never_to_safety_blocked() -> None:
    pub, meta, payload = run(draft(revision=1), profiles())
    assert [p.topic for p in pub.sent] == [DRAFTS_CLEARED]
    assert pub.log == ["publish", "flush"]
    assert meta.type == "safety.cleared" and meta.rules_version == T3_RULES_VERSION
    assert meta.correlation_id == "run-1" and meta.trace_id == "t-1"
    assert payload.draft.recommendation_id == "rec-1" and payload.draft.revision == 1
    assert payload.rules_version == T3_RULES_VERSION


def test_a_blocked_draft_goes_to_safety_blocked_with_its_violations_and_never_forward() -> None:
    zone5 = claim("Train in zone 5.", "target_hr_zone", 5.0, "zone")
    pub, meta, payload = run(draft("Train in zone 5.", [zone5]), profiles(frozenset({"cardiac"})))
    assert [p.topic for p in pub.sent] == [SAFETY_BLOCKED]
    assert meta.type == "recommendation.blocked"
    assert any(v.rule == "intensity_cap" for v in payload.violations)
    assert payload.rules_version == T3_RULES_VERSION


def test_an_unknown_user_is_blocked_not_cleared() -> None:
    pub, _, payload = run(draft(), InMemoryProfileStore())
    assert [p.topic for p in pub.sent] == [SAFETY_BLOCKED]
    assert payload.violations[0].rule == "profile_known"


def test_each_revision_has_its_own_deterministic_output_id() -> None:
    _, m0, _ = run(draft(revision=0), profiles())
    _, m1, _ = run(draft(revision=1), profiles())
    assert m0.event_id != m1.event_id


def test_other_event_types_are_ignored() -> None:
    pub = RecordingPublisher()
    meta, d = event(draft())
    make_guardian_handler(profiles(), PlainSerde(), pub)(
        meta.model_copy(update={"type": "safety.cleared"}), d
    )
    assert pub.sent == []
