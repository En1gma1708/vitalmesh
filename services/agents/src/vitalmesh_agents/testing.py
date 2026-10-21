"""Stand-ins shared by the agents' tests and by offline evaluation runs (no network, no quota)."""

from __future__ import annotations

import json

from vitalmesh_evidence.types import EvidenceAnswer
from vitalmesh_llm import LLMRequest, LLMResponse, ToolCall

TOOL_FIRST = {
    "nutrition-specialist": "get_energy_target",
    "movement-specialist": "get_training_targets",
    "condition-specialist": "get_constraints",
}


def reply(text: str, calls: tuple[ToolCall, ...] = ()) -> LLMResponse:
    return LLMResponse(
        text=text,
        provider="scripted",
        model="scripted",
        prompt_tokens=100,
        completion_tokens=20,
        latency_ms=1.0,
        finish_reason="tool_calls" if calls else "stop",
        tool_calls=calls,
    )


class ScriptedRouter:
    """Answers by purpose: each specialist calls its first tool, then gives a number-free text; the
    Synthesizer writes a fixed reply; the Checker approves (or follows ``verdicts``); the Briefer
    writes one sentence. Counts calls per purpose."""

    def __init__(self, verdicts: list[str] | None = None) -> None:
        self.purposes: list[str] = []
        self._verdicts = list(verdicts or [])

    def complete(self, req: LLMRequest, max_wait_s: float = 0.0) -> LLMResponse:
        self.purposes.append(req.purpose)
        if req.purpose in TOOL_FIRST:
            if any(m.role == "tool" for m in req.messages):
                return reply("This follows your own targets.")
            return reply("", (ToolCall(id="c0", name=TOOL_FIRST[req.purpose], arguments="{}"),))
        if req.purpose == "synthesizer":
            return reply("Here is a short plan that follows your own targets.")
        if req.purpose == "checker":
            verdict = self._verdicts.pop(0) if self._verdicts else "approve"
            problems = [] if verdict == "approve" else ["scripted problem"]
            return reply(json.dumps({"verdict": verdict, "problems": problems}))
        if req.purpose == "briefer":
            return reply("The user asked a question and the system flagged it for review.")
        raise AssertionError(f"unexpected purpose {req.purpose}")


class NoEvidence:
    def answer(self, question: str) -> EvidenceAnswer:
        return EvidenceAnswer(status="insufficient_evidence")
