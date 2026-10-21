from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from vitalmesh_evidence.types import EvidenceAnswer
from vitalmesh_llm import AllProvidersFailed, LLMRequest, LLMResponse, ToolCall
from vitalmesh_specialists import (
    NutritionSpecialist,
    NutritionTools,
    SpecialistUnavailable,
    ToolResult,
    check_answer,
)
from vitalmesh_specialists.nutrition import MAX_CALLS_PER_ROUND, MAX_MODEL_CALLS
from vitalmesh_tracing.testing import RecordingTracer
from vitalmesh_wellness.engine import Profile

PROFILE = Profile.model_validate(
    {
        "user_id": "t",
        "sex": "female",
        "age_years": 30,
        "weight_kg": 65,
        "height_cm": 165,
        "activity": "moderate",
        "resting_hr_bpm": 60,
        "goal": "maintain",
    }
)


class NoEvidence:
    def answer(self, question: str) -> EvidenceAnswer:
        return EvidenceAnswer(status="insufficient_evidence")


def tools() -> NutritionTools:
    return NutritionTools(PROFILE, [8.0] * 14, NoEvidence(), date(2026, 10, 26))


def resp(text: str, calls: tuple[ToolCall, ...] = ()) -> LLMResponse:
    return LLMResponse(
        text=text,
        provider="fake",
        model="fake",
        prompt_tokens=100,
        completion_tokens=20,
        latency_ms=1.0,
        finish_reason="tool_calls" if calls else "stop",
        tool_calls=calls,
    )


def asks(*names: str) -> LLMResponse:
    calls = tuple(ToolCall(id=f"c{i}", name=n, arguments="{}") for i, n in enumerate(names))
    return resp("", calls)


class FakeRouter:
    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.requests: list[LLMRequest] = []

    def complete(self, req: LLMRequest, max_wait_s: float = 0.0) -> LLMResponse:
        self.requests.append(req)
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, Exception):
            raise r
        assert isinstance(r, LLMResponse)
        return r


def run(router: FakeRouter, q: str = "What should I eat?") -> Any:
    return NutritionSpecialist(router).advise(q, tools())


GOOD = "Your daily target is about 2124 kcal, and you should train in zone 3."


def test_a_tool_round_then_a_verified_answer() -> None:
    r = FakeRouter(asks("get_energy_target"), resp(GOOD))
    out = run(r)
    assert out.status == "answered" and out.text == GOOD
    assert out.tools_used == ("get_energy_target",) and out.model_calls == 2
    assert (out.prompt_tokens, out.completion_tokens) == (200, 40)
    first, second = r.requests
    names = [t.name for t in first.tools]
    assert names == ["get_energy_target", "get_meal_plan", "search_guidelines"]
    msgs = second.messages
    assert msgs[2].role == "assistant" and msgs[2].tool_calls[0].id == "c0"
    assert msgs[3].role == "tool" and msgs[3].tool_call_id == "c0"
    assert json.loads(msgs[3].content)["claims"][0]["metric"] == "daily_energy_target"


def test_a_number_that_no_tool_returned_is_sent_back_once_with_the_problem() -> None:
    r = FakeRouter(asks("get_energy_target"), resp("Your target is 1900 kcal."), resp(GOOD))
    out = run(r)
    assert out.status == "answered" and out.text == GOOD and out.model_calls == 3
    feedback = r.requests[2].messages[-1]
    assert feedback.role == "user" and "1900" in feedback.content
    assert "failed these checks" in feedback.content


def test_two_bad_drafts_become_a_deterministic_fallback_built_from_tool_results() -> None:
    r = FakeRouter(asks("get_energy_target"), resp("Eat 1500 kcal."), resp("Really, 1500 kcal."))
    out = run(r)
    assert out.status == "fallback" and "1500" not in out.text
    assert "not in any tool result" in out.reason
    assert check_answer(out.text, [tools().call("get_energy_target", "{}")]) == []


def test_a_question_that_needs_no_tool_is_answered_only_if_the_text_has_no_numbers() -> None:
    ok = run(FakeRouter(resp("I cannot help with medication. Please speak to a doctor.")))
    assert ok.status == "answered" and ok.tools_used == () and ok.model_calls == 1
    bad = run(FakeRouter(resp("Take 500 mg daily.")))
    assert bad.status == "fallback" and "500" not in bad.text
    assert "could not find checked information" in bad.text


def test_no_more_tool_calls_run_per_round_than_the_limit() -> None:
    r = FakeRouter(asks(*["get_energy_target"] * 5), resp(GOOD))
    out = run(r)
    tool_msgs = [m for m in r.requests[1].messages if m.role == "tool"]
    assert len(tool_msgs) == MAX_CALLS_PER_ROUND and out.status == "answered"


def test_a_model_that_never_stops_calling_tools_is_cut_off_on_the_last_call() -> None:
    r = FakeRouter(asks("get_energy_target"))  # asks for a tool on every turn
    out = run(r)
    assert len(r.requests) == MAX_MODEL_CALLS and out.model_calls == MAX_MODEL_CALLS
    assert r.requests[-1].tools == () and all(q.tools for q in r.requests[:-1])
    assert out.status == "fallback"  # the forced last turn produced no checked answer


def test_a_bad_tool_call_returns_an_error_to_the_model_and_the_loop_continues() -> None:
    call = ToolCall(id="c0", name="get_meal_plan", arguments='{"days": 99}')
    r = FakeRouter(resp("", (call,)), resp("I could not make a plan; please ask for fewer days."))
    out = run(r)
    assert "bad arguments" in r.requests[1].messages[3].content
    assert out.status == "answered" and out.tools_used == ()


def test_a_provider_failure_is_not_an_advice_verdict() -> None:
    router = FakeRouter(AllProvidersFailed(["groq: HTTP 429"]))
    with pytest.raises(SpecialistUnavailable):
        NutritionSpecialist(router, sleep=lambda _: None).advise("q", tools())
    assert len(router.requests) == 3  # tried three times before giving up


def test_the_prompt_declares_data_class_and_keeps_rules_first() -> None:
    r = FakeRouter(resp("ok, nothing to add"))
    run(r)
    req = r.requests[0]
    assert req.data_class == "synthetic" and req.messages[0].role == "system"
    assert "Every number in your answer must appear in a tool result" in req.messages[0].content


def test_check_answer_rules() -> None:
    ok = ToolResult(name="x", ok=True, payload={"v": 2123.9})
    assert check_answer("About 2124 kcal.", [ok]) == []
    assert "not in any tool result" in check_answer("About 2500 kcal.", [ok])[0]
    assert check_answer("", [ok]) and check_answer("word " * 130, [ok])
    failed = ToolResult(name="x", ok=False, error="blocked")
    assert check_answer("About 2124 kcal.", [failed])  # a failed tool supplies no numbers


def test_a_run_is_traced_with_tool_spans_and_scores() -> None:
    tr = RecordingTracer()
    r = FakeRouter(asks("get_energy_target"), resp(GOOD))
    NutritionSpecialist(r, tracer=tr).advise("Target?", tools(), user_id="demo-1")
    assert [(x.name, x.kind) for x in tr.records] == [
        ("advise-nutrition", "run"),
        ("get_energy_target", "tool"),
        ("verify-advice", "guardrail"),
    ]
    assert tr.records[1].parent == "advise-nutrition"
    assert ("advice-verified", True) in tr.scores
    assert ("nutrition-status", "answered") in tr.scores
    assert tr.records[0].attrs["tags"] == ["nutrition"]


def test_thousands_written_with_a_no_break_space_are_one_number() -> None:
    from vitalmesh_specialists.nutrition import numbers

    assert numbers("Total: 2\u202f235 kcal") == {2235.0}
    assert numbers("about 2\u00a0124 and 2,235") == {2124.0, 2235.0}
    assert numbers("week 2 150 minutes") == {2.0, 150.0}  # a plain space separates two numbers
    ok = ToolResult(name="x", ok=True, payload={"total": 2235})
    assert check_answer("Day total: 2\u202f235 kcal.", [ok]) == []


def test_a_provider_outage_is_retried_with_a_pause_before_giving_up() -> None:
    sleeps: list[float] = []
    fail = AllProvidersFailed(["groq: cooling down"])
    r = FakeRouter(fail, fail, resp("Please speak to a doctor about that."))
    out = NutritionSpecialist(r, retry_pause_s=7.0, sleep=sleeps.append).advise("q", tools())
    assert out.status == "answered" and sleeps == [7.0, 7.0]
    with pytest.raises(SpecialistUnavailable):
        NutritionSpecialist(FakeRouter(fail), sleep=sleeps.append).advise("q", tools())


def test_a_reply_cut_off_at_the_token_limit_is_asked_again_with_more_room() -> None:
    cut = LLMResponse(
        text="",
        provider="f",
        model="f",
        prompt_tokens=1,
        completion_tokens=900,
        latency_ms=0.0,
        finish_reason="length",
    )
    r = FakeRouter(cut, resp("Please speak to a doctor about that."))
    out = run(r)
    assert [q.max_tokens for q in r.requests] == [900, 1800] and out.status == "answered"
