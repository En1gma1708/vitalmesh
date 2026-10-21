"""Deterministic tools for the Nutrition specialist (D65).

Every number the specialist may say comes from one of these tools: the energy target, zone and
sleep claims from the rule engine, the meal plan from the planner, both checked by the Guardian
before the tool returns anything, and guideline sentences from the Evidence agent with their
citations. The model chooses WHICH tool to call; it never computes. Tool arguments are
validated here because the model is untrusted: an unknown tool or bad argument returns an error
result, never an exception into the graph.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from vitalmesh_evidence.types import EvidenceAnswer
from vitalmesh_llm import ToolSpec
from vitalmesh_wellness.calculators import OutOfRange
from vitalmesh_wellness.engine import (
    Profile,
    build_meal_plan_recommendation,
    build_recommendation,
)
from vitalmesh_wellness.guardian import Guardian
from vitalmesh_wellness.mealplan import PlanError
from vitalmesh_wellness.recipes import BY_ID

MAX_PLAN_DAYS = 7


class EvidenceLike(Protocol):
    def answer(self, question: str) -> EvidenceAnswer: ...


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    ok: bool
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str = ""

    def to_message_content(self) -> str:
        body = self.payload if self.ok else {"error": self.error}
        return json.dumps(body, ensure_ascii=False, sort_keys=True)


class _PlanArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int = Field(default=3, ge=1, le=MAX_PLAN_DAYS)


class _SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=300)


SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_energy_target",
        description=(
            "The user's OWN computed numbers: daily calories (kcal), heart-rate training zone "
            "(bpm) and hours of sleep to aim for, from the deterministic rule engine. Use it for "
            "any question about how many calories, which training zone or how much sleep the "
            "user personally needs. Takes no arguments."
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolSpec(
        name="get_meal_plan",
        description=(
            "A Guardian-checked meal plan for the user's diet and allergens: per day the meals, "
            "portions and kcal. Use it for what to eat."
        ),
        parameters={
            "type": "object",
            "properties": {"days": {"type": "integer", "minimum": 1, "maximum": MAX_PLAN_DAYS}},
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="search_guidelines",
        description=(
            "Ask the guideline evidence agent a general nutrition or physical-activity question. "
            "Returns cited sentences from US federal guideline documents, or says the evidence "
            "is insufficient. Use it for general recommendations, never for the user's own numbers."
        ),
        parameters={
            "type": "object",
            "properties": {"question": {"type": "string", "maxLength": 300}},
            "required": ["question"],
            "additionalProperties": False,
        },
    ),
)


class NutritionTools:
    def __init__(
        self,
        profile: Profile,
        recent_sleep_hours: list[float],
        evidence: EvidenceLike,
        start: date,
        guardian: Guardian | None = None,
    ) -> None:
        self._profile = profile
        self._sleep = recent_sleep_hours
        self._evidence = evidence
        self._start = start
        self._guardian = guardian or Guardian()

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return SPECS

    def call(self, name: str, arguments: str) -> ToolResult:
        try:
            raw: Any = json.loads(arguments) if arguments.strip() else {}
            if not isinstance(raw, dict):
                raise ValueError("arguments must be a JSON object")
            if name == "get_energy_target":
                if raw:
                    raise ValueError("this tool takes no arguments")
                return self._energy()
            if name == "get_meal_plan":
                return self._plan(_PlanArgs.model_validate(raw).days)
            if name == "search_guidelines":
                return self._search(_SearchArgs.model_validate(raw).question)
        except (ValueError, ValidationError) as exc:
            return ToolResult(name=name, ok=False, error=f"bad arguments: {str(exc)[:120]}")
        return ToolResult(name=name, ok=False, error=f"unknown tool '{name}'")

    # ----------------------------------------------------------------------------- tools
    def _energy(self) -> ToolResult:
        try:
            rec = build_recommendation(self._profile, self._sleep, "specialist")
        except OutOfRange:
            return ToolResult(name="get_energy_target", ok=False, error="profile out of range")
        if not self._guardian.evaluate(rec).cleared:
            return ToolResult(name="get_energy_target", ok=False, error="blocked by safety rules")
        claims = [
            {
                "metric": c.quantity.metric,
                "value": c.quantity.value,
                "unit": c.quantity.unit,
                "text": c.text,
            }
            for c in rec.claims
            if c.quantity
        ]
        return ToolResult(name="get_energy_target", ok=True, payload={"claims": claims})

    def _plan(self, days: int) -> ToolResult:
        try:
            rec = build_meal_plan_recommendation(
                self._profile, self._sleep, "specialist", self._start, days
            )
        except (PlanError, OutOfRange):
            return ToolResult(name="get_meal_plan", ok=False, error="no safe plan is available")
        if not self._guardian.evaluate(rec).cleared:
            return ToolResult(name="get_meal_plan", ok=False, error="blocked by safety rules")
        by_day: dict[date, list[dict[str, Any]]] = {}
        for e in rec.meal_plan:
            r = BY_ID[e.recipe_id]
            by_day.setdefault(e.day, []).append(
                {
                    "meal": e.meal,
                    "title": r.title,
                    "portion": e.scale,
                    "kcal": round(r.kcal * e.scale),
                }
            )
        out = [
            {
                "day": (self._start + timedelta(days=i)).isoformat(),
                "meals": by_day.get(d, []),
                "total_kcal": sum(m["kcal"] for m in by_day.get(d, [])),
            }
            for i, d in enumerate(sorted(by_day))
        ]
        return ToolResult(name="get_meal_plan", ok=True, payload={"days": out})

    def _search(self, question: str) -> ToolResult:
        ans = self._evidence.answer(question)
        if ans.status != "supported":
            return ToolResult(
                name="search_guidelines", ok=True, payload={"status": ans.status, "sentences": []}
            )
        sentences = [
            {
                "text": s.text,
                "doc_id": s.citation.doc_id,
                "page": s.citation.locator,
                "licence": s.citation.licence,
                "quote": s.citation.quote,
            }
            for s in ans.sentences
        ]
        return ToolResult(
            name="search_guidelines",
            ok=True,
            payload={"status": "supported", "sentences": sentences},
        )
