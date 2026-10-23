"""Stand-ins shared by the agents' tests and by offline evaluation runs (no network, no quota)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

from vitalmesh_common.run import Specialist
from vitalmesh_evidence.types import EvidenceAnswer
from vitalmesh_llm import LLMRequest, LLMResponse, ToolCall
from vitalmesh_stores import (
    InMemoryHistoryStore,
    InMemoryLabStore,
    InMemoryProfileStore,
    ProfileRecord,
    RunLog,
)
from vitalmesh_wellness.engine import Profile
from vitalmesh_wellness.labs import interpret

from vitalmesh_agents.cohortstore import CohortStore
from vitalmesh_agents.inprocess import Bus, Wiring, build_graph
from vitalmesh_agents.specialist_handler import SpecialistDeps
from vitalmesh_agents.triage import Load

TOOL_FIRST = {
    "nutrition-specialist": "get_energy_target",
    "movement-specialist": "get_training_targets",
    "condition-specialist": "get_constraints",
    "single-specialist": "get_energy_target",
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

    def __init__(self, verdicts: list[str] | None = None, numeric: bool = False) -> None:
        self.purposes: list[str] = []
        self._verdicts = list(verdicts or [])
        self._numeric = numeric  # the Synthesizer restates the energy target it was given

    def complete(self, req: LLMRequest, max_wait_s: float = 0.0) -> LLMResponse:
        self.purposes.append(req.purpose)
        if req.purpose in TOOL_FIRST:
            if any(m.role == "tool" for m in req.messages):
                return reply("This follows your own targets.")
            return reply("", (ToolCall(id="c0", name=TOOL_FIRST[req.purpose], arguments="{}"),))
        if req.purpose == "synthesizer":
            if self._numeric:
                found = re.search(
                    r"about (\d[\d,]*) kcal", " ".join(m.content or "" for m in req.messages)
                )
                if found:
                    return reply(f"Aim for about {found.group(1)} kcal per day.")
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


T0 = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def world(
    conditions: list[str] | None = None,
    verdicts: list[str] | None = None,
    specialists: frozenset[Specialist] = frozenset(Specialist),
    labs: bool = False,
    cohort: CohortStore | None = None,
    numeric: bool = False,
    load: Load = Load.NORMAL,
    extra_users: int = 0,
    runlog: RunLog | None = None,
) -> tuple[Bus, ScriptedRouter, Clock]:
    profiles = InMemoryProfileStore()
    profiles.put(
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
                    "conditions": conditions or [],
                }
            ),
            recent_sleep_hours=tuple([8.0] * 14),
        )
    )
    # a second user of the same archetype (same sex, age band, activity) but a different body
    profiles.put(
        ProfileRecord(
            profile=Profile.model_validate(
                {
                    "user_id": "u-2",
                    "sex": "female",
                    "age_years": 34,
                    "weight_kg": 80,
                    "height_cm": 170,
                    "activity": "moderate",
                    "resting_hr_bpm": 58,
                    "conditions": conditions or [],
                }
            ),
            recent_sleep_hours=tuple([7.0] * 14),
        )
    )
    # synthetic cohorts for benchmarks: three archetypes, bodies that differ user to user
    groups = [("female", 34), ("male", 34), ("female", 24)]
    for i in range(extra_users):
        sex, age = groups[i % len(groups)]
        profiles.put(
            ProfileRecord(
                profile=Profile.model_validate(
                    {
                        "user_id": f"b-{i}",
                        "sex": sex,
                        "age_years": age,
                        "weight_kg": 55 + (i * 3) % 40,
                        "height_cm": 160 + (i % 5) * 5,
                        "activity": "moderate",
                        "resting_hr_bpm": 52 + i % 12,
                    }
                ),
                recent_sleep_hours=tuple([7.0 + (i % 3) * 0.5] * 14),
            )
        )
    lab_store = InMemoryLabStore()
    if labs:
        lab_store.put(
            "u-1",
            interpret(
                "LDL Cholesterol",
                T0 - timedelta(days=3),
                value=190,
                unit="mg/dL",
                reference_text="< 100",
            ),
        )
    router, clock = ScriptedRouter(verdicts, numeric), Clock()
    deps = SpecialistDeps(
        router=router,
        evidence=NoEvidence(),
        profiles=profiles,
        labs=lab_store,
        history=InMemoryHistoryStore(),
        sleep=lambda _s: None,
    )
    bus = build_graph(
        Wiring(
            router=router,
            deps=deps,
            profiles=profiles,
            clock=clock,
            specialists=specialists,
            cohort=cohort,
            load=load,
            runlog=runlog,
        )
    )
    return bus, router, clock
