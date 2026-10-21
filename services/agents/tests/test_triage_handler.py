from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from vitalmesh.events.v1 import common_pb2, payloads_pb2
from vitalmesh_agents.events import derived_id
from vitalmesh_agents.triage import Load
from vitalmesh_agents.triage_handler import UnknownUser, make_triage_handler
from vitalmesh_common import codec
from vitalmesh_common.envelope import EventMeta, Tier
from vitalmesh_common.testing import PlainSerde, RecordingPublisher
from vitalmesh_common.topics import RUNS_PLANNED
from vitalmesh_stores import InMemoryLabStore, InMemoryProfileStore, ProfileRecord
from vitalmesh_wellness.labs import interpret
from vitalmesh_wellness.synthetic import synthetic_user

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


def question(user_id: str, text: str, event_id: str = "q-1") -> tuple[EventMeta, Any]:
    meta = EventMeta(
        event_id=event_id,
        correlation_id="run-1",
        user_id=user_id,
        household_id="h-1",
        type="question.asked",
        occurred_at=NOW,
        trace_id="trace-1",
    )
    return meta, payloads_pb2.QuestionAsked(question=text, session_id="s-1")


def setup(conditions: frozenset[str] = frozenset()) -> tuple[InMemoryProfileStore, str]:
    profile, sleep = synthetic_user(7, 1)
    profile = profile.model_copy(update={"conditions": conditions})
    store = InMemoryProfileStore()
    store.put(ProfileRecord(profile=profile, recent_sleep_hours=tuple(sleep)))
    return store, profile.user_id


def run(
    store: InMemoryProfileStore,
    labs: InMemoryLabStore,
    uid: str,
    text: str,
    load: Load = Load.NORMAL,
) -> tuple[RecordingPublisher, EventMeta, Any]:
    pub = RecordingPublisher()
    handler = make_triage_handler(
        store, labs, PlainSerde(), pub, load=lambda: load, clock=lambda: NOW
    )
    handler(*question(uid, text))
    meta, payload = codec.decode(pub.sent[0].value)
    return pub, meta, payload


def test_plan_is_published_to_runs_planned_with_tier_and_deadline() -> None:
    store, uid = setup()
    pub, meta, plan = run(
        store, InMemoryLabStore(), uid, "What should I eat and how should I train?"
    )
    assert [p.topic for p in pub.sent] == [RUNS_PLANNED]
    assert pub.sent[0].key == uid
    assert pub.log == ["publish", "flush"]
    assert meta.type == "run.planned"
    assert meta.tier is Tier.T3
    assert meta.deadline_at == NOW + timedelta(seconds=90)
    assert meta.correlation_id == "run-1" and meta.trace_id == "trace-1"
    assert meta.causation_id == "q-1"
    assert meta.event_id == derived_id("q-1", "run.planned")  # redelivery re-publishes the same id
    assert list(plan.specialists) == [
        common_pb2.SPECIALIST_NUTRITION,
        common_pb2.SPECIALIST_MOVEMENT_RECOVERY,
    ]
    assert plan.question == "What should I eat and how should I train?"
    assert plan.max_model_calls == 5
    assert "route.nutrition_terms" in plan.rule_ids


def test_profile_conditions_and_labs_shape_the_plan() -> None:
    store, uid = setup(frozenset({"cardiac"}))
    labs = InMemoryLabStore()
    labs.put(
        uid,
        interpret(
            "LDL Cholesterol",
            NOW - timedelta(days=3),
            value=190,
            unit="mg/dL",
            reference_text="< 100",
        ),
    )
    _, meta, plan = run(store, labs, uid, "Give me a workout")
    assert plan.risk == common_pb2.RISK_HIGH
    assert common_pb2.SPECIALIST_CONDITION_MEDICATION in plan.specialists
    assert meta.tier is Tier.T3


def test_high_load_degrades_the_published_plan() -> None:
    store, uid = setup(frozenset({"cardiac"}))
    _, _, plan = run(
        store, InMemoryLabStore(), uid, "My cholesterol, my meals and my workouts", Load.HIGH
    )
    assert plan.degraded
    assert plan.max_model_calls == 3
    assert len(plan.specialists) == 2


def test_unknown_user_is_a_retryable_error_not_a_guessed_plan() -> None:
    pub = RecordingPublisher()
    handler = make_triage_handler(InMemoryProfileStore(), InMemoryLabStore(), PlainSerde(), pub)
    with pytest.raises(UnknownUser):
        handler(*question("nobody", "What should I eat?"))
    assert pub.sent == []


def test_other_event_types_are_ignored() -> None:
    store, uid = setup()
    meta, payload = question(uid, "What should I eat?")
    pub = RecordingPublisher()
    handler = make_triage_handler(store, InMemoryLabStore(), PlainSerde(), pub)
    handler(meta.model_copy(update={"type": "run.planned"}), payload)
    assert pub.sent == []


def test_a_failed_flush_propagates_so_the_offset_is_not_committed() -> None:
    store, uid = setup()
    pub = RecordingPublisher(fail_flush=True)
    handler = make_triage_handler(store, InMemoryLabStore(), PlainSerde(), pub, clock=lambda: NOW)
    with pytest.raises(RuntimeError):
        handler(*question(uid, "What should I eat?"))
