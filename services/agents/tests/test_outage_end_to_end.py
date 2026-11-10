"""With no model at all, a question is still answered from the tools, not refused (D100).

Seen live: a helper reported ``llm.unavailable`` after zero model calls, the run closed with no
claims at all, and the Guardian rule ``has_claims`` refused the question for the user ("I can't
give this answer"). The helpers now fall back to their pure-code tools, so claims exist and the
answer goes out, marked as built from the tools.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from vitalmesh_agents.inprocess import ask
from vitalmesh_agents.testing import T0, TOOL_FIRST, ScriptedRouter, world
from vitalmesh_llm import AllProvidersFailed, LLMRequest, LLMResponse


def fail_when(monkeypatch: pytest.MonkeyPatch, down: Callable[[str], bool]) -> None:
    original = ScriptedRouter.complete

    def complete(self: ScriptedRouter, req: LLMRequest, max_wait_s: float = 0.0) -> LLMResponse:
        if down(req.purpose):
            raise AllProvidersFailed(["groq: HTTP 429", "gemini: HTTP 503"])
        return original(self, req, max_wait_s)

    monkeypatch.setattr(ScriptedRouter, "complete", complete)


def advice_statuses(out: Any) -> dict[str, str]:
    from vitalmesh.events.v1 import payloads_pb2

    names = {
        payloads_pb2.ADVICE_ANSWERED: "answered",
        payloads_pb2.ADVICE_FALLBACK: "fallback",
        payloads_pb2.ADVICE_DECLINED: "declined",
        payloads_pb2.ADVICE_FAILED: "FAILED",
    }
    return {
        str(s.payload.specialist): names[s.payload.status]
        for s in out.events
        if s.meta.type == "advice.completed"
    }


def test_when_every_helpers_model_is_down_the_question_is_still_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fail_when(monkeypatch, lambda purpose: purpose in TOOL_FIRST)
    bus, _router, _clock = world()
    out = ask(bus, "u-1", "What should I eat and how should I train?", T0)
    assert out.final == "released", out.final
    assert out.errors == ()
    statuses = set(advice_statuses(out).values())
    assert statuses == {"fallback"}  # every helper answered from its tools; none FAILED
    released = next(s for s in out.events if s.meta.type == "recommendation.released")
    assert len(released.payload.claims) > 0  # real computed claims, not an empty answer
    assert out.text.strip()


def test_one_helper_down_and_the_others_fine_is_still_a_complete_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fail_when(monkeypatch, lambda purpose: purpose == "movement-specialist")
    bus, _router, _clock = world()
    out = ask(bus, "u-1", "What should I eat and how should I train?", T0)
    assert out.final == "released"
    statuses = advice_statuses(out)
    assert sorted(statuses.values()) == ["answered", "fallback"]
    released = next(s for s in out.events if s.meta.type == "recommendation.released")
    assert released.payload.partial is False  # nothing is missing: the part came from the tools


def test_when_every_model_call_fails_the_answer_is_built_from_the_checked_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fail_when(monkeypatch, lambda purpose: True)
    bus, _router, _clock = world()
    out = ask(bus, "u-1", "What should I eat and how should I train?", T0)
    assert out.final == "released", (out.final, out.errors)
    assert out.text.strip()
    drafted = next(s for s in out.events if s.meta.type == "recommendation.drafted")
    assert drafted.payload.used_fallback is True  # the writer was down too: notes, not prose
