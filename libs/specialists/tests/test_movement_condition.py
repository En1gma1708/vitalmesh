from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from vitalmesh_evidence.types import AnswerSentence, Citation, EvidenceAnswer
from vitalmesh_llm import LLMRequest, LLMResponse, ToolCall
from vitalmesh_specialists.condition import (
    ConditionSpecialist,
    ConditionTools,
    latest_per_marker,
)
from vitalmesh_specialists.condition import fallback_text as condition_fallback
from vitalmesh_specialists.movement import MovementSpecialist, MovementTools
from vitalmesh_specialists.movement import fallback_text as movement_fallback
from vitalmesh_stores import InMemoryHistoryStore, Point
from vitalmesh_wellness.engine import Profile
from vitalmesh_wellness.labs import interpret

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)  # a Thursday


def profile(**extra: Any) -> Profile:
    return Profile.model_validate(
        {
            "user_id": "t",
            "sex": "female",
            "age_years": 30,
            "weight_kg": 65,
            "height_cm": 165,
            "activity": "moderate",
            "resting_hr_bpm": 60,
            "goal": "maintain",
            **extra,
        }
    )


class FakeEvidence:
    def __init__(self, answer: EvidenceAnswer | None = None) -> None:
        self.answer_ = answer or EvidenceAnswer(status="insufficient_evidence")
        self.questions: list[str] = []

    def answer(self, question: str) -> EvidenceAnswer:
        self.questions.append(question)
        return self.answer_


SUPPORTED = EvidenceAnswer(
    status="supported",
    sentences=(
        AnswerSentence(
            text="Adults need at least 150 minutes of moderate activity a week.",
            citation=Citation(
                doc_id="pag-2018",
                locator="p. 34",
                title="PAG",
                source_url="https://x.org",
                licence="US federal government work",
                quote="at least 150 minutes a week of moderate-intensity aerobic activity",
            ),
        ),
    ),
)


def movement(history: InMemoryHistoryStore | None = None, **extra: Any) -> MovementTools:
    return MovementTools(
        profile(**extra), [8.0] * 14, history or InMemoryHistoryStore(), FakeEvidence(), NOW
    )


def payload(tools: Any, name: str, args: str = "{}") -> dict[str, Any]:
    result = tools.call(name, args)
    assert result.ok, result.error
    return dict(result.payload)


# ------------------------------------------------------------------------------ movement
def test_training_targets_come_from_the_engine_with_all_five_zones() -> None:
    out = payload(movement(), "get_training_targets")
    metrics = {c["metric"]: c for c in out["claims"]}
    assert set(metrics) == {"target_hr_zone", "sleep_target"}
    assert metrics["target_hr_zone"]["unit"] == "zone" and metrics["sleep_target"]["unit"] == "h"
    assert all(c["source"].startswith("vitalmesh_wellness.engine@") for c in out["claims"])
    assert [z["zone"] for z in out["zones"]] == [1, 2, 3, 4, 5]
    assert all(z["low_bpm"] < z["high_bpm"] for z in out["zones"])


def test_a_cardiac_user_is_never_given_a_zone_above_the_graph_cap() -> None:
    out = payload(movement(conditions=["cardiac"]), "get_training_targets")
    zone = next(c for c in out["claims"] if c["metric"] == "target_hr_zone")
    assert zone["value"] <= 3


def history_with_weeks() -> InMemoryHistoryStore:
    h = InMemoryHistoryStore()
    # Mon 2026-10-12 .. Sun 2026-10-18 is the last COMPLETE week; Mon 10-19 starts this one.
    for day, minutes in ((12, 30.0), (14, 40.0), (16, 50.0), (19, 20.0), (21, 25.0)):
        h.add("t", "active_minutes", Point(datetime(2026, 10, day, 8, tzinfo=UTC), minutes, "min"))
    for night in range(7):
        h.add("t", "sleep_duration", Point(NOW - timedelta(days=night + 1), 7.0, "h"))
    return h


def test_activity_summary_judges_the_last_full_week_not_the_partial_one() -> None:
    out = payload(movement(history_with_weeks()), "get_activity_summary")
    weeks = {w["week_starting"]: w["active_minutes"] for w in out["weeks"]}
    assert weeks == {"2026-10-12": 120, "2026-10-19": 45}
    weekly = next(c for c in out["claims"] if c["metric"] == "weekly_active_minutes")
    assert weekly["value"] == 120.0  # not 45: the current week is not finished
    assert "2026-10-12" in weekly["text"] and "150" in weekly["text"]
    sleep = next(c for c in out["claims"] if c["metric"] == "average_sleep_7d")
    assert (sleep["value"], sleep["unit"]) == (7.0, "h")


def test_no_history_is_said_not_invented() -> None:
    out = payload(movement(), "get_activity_summary")
    assert out["available"] is False and out["claims"] == []


@pytest.mark.parametrize("name", ["get_training_targets", "get_activity_summary"])
def test_movement_tools_refuse_arguments_and_unknown_tools(name: str) -> None:
    assert not movement().call(name, '{"x": 1}').ok
    assert not movement().call("rm_rf", "{}").ok
    assert not movement().call("search_guidelines", "{}").ok  # question is required


def test_movement_search_goes_through_the_evidence_agent_and_keeps_the_citation() -> None:
    ev = FakeEvidence(SUPPORTED)
    tools = MovementTools(profile(), [8.0] * 14, InMemoryHistoryStore(), ev, NOW)
    out = payload(tools, "search_guidelines", '{"question": "how much activity do adults need"}')
    assert ev.questions == ["how much activity do adults need"]
    assert out["sentences"][0]["page"] == "p. 34"


def test_movement_fallback_uses_tool_results_only() -> None:
    results = [movement().call("get_training_targets", "{}")]
    text = movement_fallback(results)
    assert "zone" in text and "sleep" in text.lower()
    assert "could not find" in movement_fallback([])


# ------------------------------------------------------------------------------ condition
def lab(name: str, value: float, reference: str, day: int = 1, unit: str = "mg/dL") -> Any:
    return interpret(
        name,
        datetime(2026, 10, day, tzinfo=UTC),
        value=value,
        unit=unit,
        reference_text=reference,
    )


def condition(labs: list[Any] | None = None, **extra: Any) -> ConditionTools:
    return ConditionTools(profile(**extra), labs or [], FakeEvidence())


def test_constraints_report_the_graph_limits_and_what_it_does_not_know() -> None:
    out = payload(
        condition(conditions=["cardiac", "celiac_disease", "some_rare_syndrome"]),
        "get_constraints",
    )
    assert out["max_training_zone"] == 3
    assert out["avoid_allergens"] == ["gluten"]
    assert out["conditions_unknown_to_the_graph"] == ["some_rare_syndrome"]  # unknown is not safe
    assert "illustrative" in out["graph"]
    claim = out["claims"][0]
    assert (claim["metric"], claim["value"], claim["unit"]) == ("max_hr_zone", 3.0, "zone")


def test_no_conditions_means_no_limit_claims() -> None:
    out = payload(condition(), "get_constraints")
    assert out["max_training_zone"] is None and out["claims"] == []


def test_lab_summary_flags_abnormal_and_marked_with_a_clinician_note() -> None:
    labs = [
        lab("LDL Cholesterol", 190, "< 100"),
        lab("HDL Cholesterol", 55, "40 - 60"),
        lab("Triglycerides", 160, "< 150"),
    ]
    out = payload(condition(labs), "get_lab_summary")
    by_marker = {r["marker"]: r for r in out["labs"]}
    assert by_marker["LDL Cholesterol"]["status"] == "marked"
    assert by_marker["HDL Cholesterol"]["status"] == "normal"
    assert by_marker["Triglycerides"]["status"] == "abnormal"
    assert len(out["claims"]) == 2  # only the out-of-range ones become claims
    assert all("clinician" in c["text"] for c in out["claims"])
    assert all(c["source"].endswith("labs-1") for c in out["claims"])


def test_only_the_latest_result_per_marker_is_summarised() -> None:
    labs = [
        lab("LDL Cholesterol", 190, "< 100", day=1),
        lab("LDL Cholesterol", 90, "< 100", day=20),
    ]
    assert [r.value for r in latest_per_marker(labs)] == [90]
    out = payload(condition(labs), "get_lab_summary")
    assert out["claims"] == [] and out["labs"][0]["status"] == "normal"


def test_an_unparsed_range_is_not_classified_never_normal() -> None:
    out = payload(condition([lab("LDL Cholesterol", 128, "Desirable: <100")]), "get_lab_summary")
    assert out["labs"][0]["status"] == "unknown" and out["claims"] == []


def test_no_labs_is_said_not_invented() -> None:
    assert payload(condition(), "get_lab_summary")["available"] is False


def test_condition_fallback_says_what_the_graph_does_not_cover() -> None:
    tools = condition(conditions=["some_rare_syndrome"])
    text = condition_fallback([tools.call("get_constraints", "{}")])
    assert "some_rare_syndrome" in text and "clinician" in text


# ---------------------------------------------------------------------- the shared loop
def resp(text: str, calls: tuple[ToolCall, ...] = ()) -> LLMResponse:
    return LLMResponse(
        text=text,
        provider="fake",
        model="fake",
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=1.0,
        finish_reason="tool_calls" if calls else "stop",
        tool_calls=calls,
    )


class FakeRouter:
    def __init__(self, *replies: LLMResponse) -> None:
        self.replies = list(replies)
        self.requests: list[LLMRequest] = []

    def complete(self, req: LLMRequest, max_wait_s: float = 0.0) -> LLMResponse:
        self.requests.append(req)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def asks(name: str) -> LLMResponse:
    return resp("", (ToolCall(id="c0", name=name, arguments="{}"),))


def test_movement_specialist_answers_from_its_tools_and_exposes_the_results() -> None:
    router = FakeRouter(asks("get_training_targets"), resp("Train in zone 3 and sleep 8 hours."))
    advice = MovementSpecialist(router).advise("How hard should I train?", movement())
    assert advice.status == "answered"
    assert advice.tools_used == ("get_training_targets",)
    assert advice.results and advice.results[0].name == "get_training_targets"
    prompt = router.requests[0].messages[0].content
    assert "movement and recovery" in prompt and "get_training_targets" in prompt
    assert router.requests[0].purpose == "movement-specialist"


def test_a_number_the_tools_did_not_return_sends_the_movement_specialist_to_fallback() -> None:
    router = FakeRouter(asks("get_training_targets"), resp("Train at 97 bpm and sleep 11 hours."))
    advice = MovementSpecialist(router).advise("How hard should I train?", movement())
    assert advice.status == "fallback"
    assert "97" not in advice.text and "zone" in advice.text


def test_condition_specialist_prompt_forbids_diagnosis_and_medication_advice() -> None:
    router = FakeRouter(asks("get_constraints"), resp("Keep training at zone 3 or below."))
    advice = ConditionSpecialist(router).advise(
        "What does my condition limit?", condition(conditions=["cardiac"])
    )
    assert advice.status == "answered"
    prompt = router.requests[0].messages[0].content
    assert "NOT a clinician" in prompt and "medication" in prompt
    assert "unknown, not safe" in prompt


def test_a_run_can_lower_the_model_call_cap() -> None:
    reply = asks("get_constraints")
    router = FakeRouter(reply)  # the model keeps asking for tools
    advice = ConditionSpecialist(router, max_model_calls=2).advise(
        "What limits me?", condition(conditions=["cardiac"])
    )
    assert advice.model_calls == 2
    assert len(router.requests) == 2
    assert router.requests[-1].tools == ()  # the last allowed call is made without tools
    assert advice.status == "fallback"


def test_the_cap_cannot_be_raised_above_the_hard_limit_or_below_one() -> None:
    with pytest.raises(ValueError):
        MovementSpecialist(FakeRouter(resp("x")), max_model_calls=6)
    with pytest.raises(ValueError):
        ConditionSpecialist(FakeRouter(resp("x")), max_model_calls=0)


def test_tool_results_round_trip_as_json_for_the_model() -> None:
    result = movement().call("get_training_targets", "{}")
    assert json.loads(result.to_message_content())["zones"]
