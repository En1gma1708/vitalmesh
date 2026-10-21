"""System under test for the Nutrition specialist (D66): the real agent, real tools, a live model.

The SUT records every number that appeared in a tool result, so the graders can check the prose
against the tools independently of the agent's own checker.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from vitalmesh_specialists import (
    NutritionSpecialist,
    NutritionTools,
    SpecialistUnavailable,
)
from vitalmesh_specialists.nutrition import numbers
from vitalmesh_specialists.tools import EvidenceLike
from vitalmesh_wellness.engine import Profile

from vitalmesh_evals.case import EvalCase
from vitalmesh_evals.suts import SutOutput

PLAN_START = date(2026, 10, 26)


class NutritionSut:
    def __init__(self, specialist: NutritionSpecialist, evidence: EvidenceLike) -> None:
        self._specialist = specialist
        self._evidence = evidence

    def run(self, case: EvalCase) -> SutOutput:
        inp = case.input
        profile = Profile.model_validate(inp["profile"])
        tools = _RecordingTools(profile, inp.get("sleep", [8.0] * 14), self._evidence)
        started = time.perf_counter()
        try:
            advice = self._specialist.advise(inp["question"], tools)
        except SpecialistUnavailable as exc:
            return SutOutput({"status": "unavailable", "text": ""}, error=f"llm_unavailable: {exc}")
        out: dict[str, Any] = {
            "status": advice.status,
            "text": advice.text,
            "tools_used": list(advice.tools_used),
            "model_calls": advice.model_calls,
            "reason": advice.reason,
            "tool_numbers": sorted(tools.seen_numbers),
        }
        return SutOutput(
            out,
            provider="nutrition-specialist",
            prompt_tokens=advice.prompt_tokens,
            completion_tokens=advice.completion_tokens,
            latency_ms=(time.perf_counter() - started) * 1000,
        )


class _RecordingTools(NutritionTools):
    def __init__(self, profile: Profile, sleep: list[float], evidence: EvidenceLike) -> None:
        super().__init__(profile, sleep, evidence, PLAN_START)
        self.seen_numbers: set[float] = set()

    def call(self, name: str, arguments: str) -> Any:
        result = super().call(name, arguments)
        if result.ok:
            self.seen_numbers |= numbers(result.to_message_content())
        return result
