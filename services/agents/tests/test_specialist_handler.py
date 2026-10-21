from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from vitalmesh.events.v1 import common_pb2, payloads_pb2
from vitalmesh_agents.events import derived_id
from vitalmesh_agents.specialist_handler import SpecialistDeps, make_specialist_handler
from vitalmesh_common import codec
from vitalmesh_common.envelope import EventMeta, Tier
from vitalmesh_common.run import Specialist
from vitalmesh_common.testing import PlainSerde, RecordingPublisher
from vitalmesh_common.topics import ADVICE_COMPLETED
from vitalmesh_evidence.types import EvidenceAnswer
from vitalmesh_llm import AllProvidersFailed, LLMRequest, LLMResponse, ToolCall
from vitalmesh_stores import (
    InMemoryHistoryStore,
    InMemoryLabStore,
    InMemoryProfileStore,
    ProfileRecord,
)
from vitalmesh_wellness.synthetic import synthetic_user

PROFILE, SLEEP = synthetic_user(7, 1)
UID = PROFILE.user_id
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
DEADLINE = NOW + timedelta(seconds=90)


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


def asks(name: str) -> LLMResponse:
    return resp("", (ToolCall(id="c0", name=name, arguments="{}"),))


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


class NoEvidence:
    def answer(self, question: str) -> EvidenceAnswer:
        return EvidenceAnswer(status="insufficient_evidence")


def deps(router: FakeRouter, with_profile: bool = True) -> SpecialistDeps:
    profiles = InMemoryProfileStore()
    if with_profile:
        profiles.put(ProfileRecord(profile=PROFILE, recent_sleep_hours=tuple(SLEEP)))
    return SpecialistDeps(
        router=router,
        evidence=NoEvidence(),
        profiles=profiles,
        labs=InMemoryLabStore(),
        history=InMemoryHistoryStore(),
        attempts=2,
        retry_pause_s=0.0,
        sleep=lambda _s: None,
    )


def planned(*specialists: int, user_id: str = UID) -> tuple[EventMeta, Any]:
    meta = EventMeta(
        event_id="plan-1",
        correlation_id="run-1",
        user_id=user_id,
        household_id="h-1",
        type="run.planned",
        occurred_at=NOW,
        trace_id="trace-1",
        tier=Tier.T3,
        deadline_at=DEADLINE,
    )
    plan = payloads_pb2.RunPlanned(
        question="What should I eat?", risk=common_pb2.RISK_LOW, max_model_calls=3
    )
    plan.specialists.extend(specialists)
    return meta, plan


def run(
    kind: Specialist, d: SpecialistDeps, event: tuple[EventMeta, Any]
) -> tuple[RecordingPublisher, Any]:
    pub = RecordingPublisher()
    make_specialist_handler(kind, d, PlainSerde(), pub, clock=lambda: NOW)(*event)
    return pub, codec.decode(pub.sent[0].value) if pub.sent else None


def test_a_planned_specialist_answers_and_publishes_advice_with_the_run_deadline() -> None:
    router = FakeRouter(asks("get_energy_target"), resp("Aim for the calories in your plan."))
    pub, decoded = run(Specialist.NUTRITION, deps(router), planned(common_pb2.SPECIALIST_NUTRITION))
    meta, advice = decoded
    assert [p.topic for p in pub.sent] == [ADVICE_COMPLETED]
    assert pub.log == ["publish", "flush"]
    assert meta.type == "advice.completed"
    assert meta.deadline_at == DEADLINE and meta.tier is Tier.T3
    assert meta.correlation_id == "run-1" and meta.causation_id == "plan-1"
    assert meta.event_id == derived_id("plan-1", "advice.nutrition")
    assert advice.specialist == common_pb2.SPECIALIST_NUTRITION
    assert advice.status == payloads_pb2.ADVICE_ANSWERED
    assert any(c.metric == "daily_energy_target" for c in advice.claims)
    assert advice.model_calls == 2 and advice.tool_calls == 1


def test_the_plans_model_call_budget_reaches_the_specialist() -> None:
    router = FakeRouter(asks("get_energy_target"))  # keeps asking for tools
    _pub, decoded = run(
        Specialist.NUTRITION, deps(router), planned(common_pb2.SPECIALIST_NUTRITION)
    )
    assert len(router.requests) == 3  # the plan said max_model_calls = 3
    assert decoded[1].status == payloads_pb2.ADVICE_FALLBACK


def test_a_specialist_the_plan_does_not_name_stays_silent() -> None:
    router = FakeRouter(resp("unused"))
    pub, _decoded = run(
        Specialist.MOVEMENT_RECOVERY, deps(router), planned(common_pb2.SPECIALIST_NUTRITION)
    )
    assert pub.sent == [] and router.requests == []


def test_each_specialist_picks_its_own_tools() -> None:
    cases = {
        Specialist.MOVEMENT_RECOVERY: (
            "get_training_targets",
            common_pb2.SPECIALIST_MOVEMENT_RECOVERY,
        ),
        Specialist.CONDITION_MEDICATION: (
            "get_constraints",
            common_pb2.SPECIALIST_CONDITION_MEDICATION,
        ),
    }
    for kind, (tool, proto) in cases.items():
        router = FakeRouter(asks(tool), resp("Here is what the tool says."))
        _, decoded = run(kind, deps(router), planned(proto))
        names = {t.name for t in router.requests[0].tools}
        assert tool in names
        assert decoded[1].specialist == proto


def test_a_missing_profile_is_published_as_a_failed_advice_not_raised() -> None:
    router = FakeRouter(resp("unused"))
    _pub, decoded = run(
        Specialist.NUTRITION,
        deps(router, with_profile=False),
        planned(common_pb2.SPECIALIST_NUTRITION),
    )
    assert decoded[1].status == payloads_pb2.ADVICE_FAILED
    assert decoded[1].failure_code == "profile.missing"
    assert router.requests == []


def test_a_provider_outage_is_published_as_a_failed_advice_after_bounded_retries() -> None:
    router = FakeRouter(AllProvidersFailed(attempts=["groq: 503"]))
    _pub, decoded = run(
        Specialist.NUTRITION, deps(router), planned(common_pb2.SPECIALIST_NUTRITION)
    )
    assert decoded[1].status == payloads_pb2.ADVICE_FAILED
    assert decoded[1].failure_code == "llm.unavailable"
    assert len(router.requests) == 2  # attempts=2, not the interactive 3 with 30 s pauses


def test_a_failed_flush_propagates_so_the_offset_is_not_committed() -> None:
    router = FakeRouter(asks("get_energy_target"), resp("Aim for the calories in your plan."))
    pub = RecordingPublisher(fail_flush=True)
    handler = make_specialist_handler(
        Specialist.NUTRITION,
        deps(router),
        PlainSerde(),
        pub,
        clock=lambda: NOW,
    )
    with pytest.raises(RuntimeError):
        handler(*planned(common_pb2.SPECIALIST_NUTRITION))


def test_other_event_types_are_ignored() -> None:
    router = FakeRouter(resp("unused"))
    meta, plan = planned(common_pb2.SPECIALIST_NUTRITION)
    pub = RecordingPublisher()
    make_specialist_handler(Specialist.NUTRITION, deps(router), PlainSerde(), pub)(
        meta.model_copy(update={"type": "advice.completed"}), plan
    )
    assert pub.sent == []
