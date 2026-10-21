"""System under test for the Concierge (D67): screen, classification, hand-off, specialist."""

from __future__ import annotations

import time
from typing import Any

from vitalmesh_concierge import Concierge, ConciergeUnavailable, Turn
from vitalmesh_specialists.tools import EvidenceLike
from vitalmesh_wellness.engine import Profile

from vitalmesh_evals.case import EvalCase
from vitalmesh_evals.nutrition_sut import RecordingTools
from vitalmesh_evals.suts import SutOutput


class ConciergeSut:
    def __init__(self, concierge: Concierge, evidence: EvidenceLike) -> None:
        self._concierge = concierge
        self._evidence = evidence

    def run(self, case: EvalCase) -> SutOutput:
        inp = case.input
        tools = RecordingTools(
            Profile.model_validate(inp["profile"]), inp.get("sleep", [8.0] * 14), self._evidence
        )
        history = [Turn(**t) for t in inp.get("history", [])]
        started = time.perf_counter()
        try:
            reply = self._concierge.reply(inp["question"], tools, history)
        except ConciergeUnavailable as exc:
            return SutOutput({"intent": "unavailable", "text": ""}, error=f"llm_unavailable: {exc}")
        out: dict[str, Any] = {
            "intent": reply.intent,
            "status": reply.status,
            "text": reply.text,
            "reason": reply.reason,
            "model_calls": reply.model_calls,
            "handoff": reply.handoff.to if reply.handoff else None,
            "tools_used": list(reply.advice.tools_used) if reply.advice else [],
            "tool_numbers": sorted(tools.seen_numbers),
        }
        return SutOutput(
            out,
            provider="concierge",
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
