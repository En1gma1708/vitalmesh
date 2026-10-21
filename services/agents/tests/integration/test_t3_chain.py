"""The T3 multi-agent run end to end on the real stack: real Kafka, Schema Registry, Redis and the
Postgres checkpointer; the model is a scripted stand-in (no network, no quota), so this tests the
choreography, not model quality.

question.asked -> Triage -> run.planned -> specialists -> advice.completed -> Aggregator ->
advice.bundle.ready -> Synthesizer -> recommendation.drafted -> Guardian -> safety.cleared ->
(Checker -> review.completed ->) Release [-> review.requested -> review.decided] ->
recommendations.released
"""

from __future__ import annotations

import contextlib
import json
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import redis
from langgraph.checkpoint.postgres import PostgresSaver
from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_agents.aggregator import Aggregator
from vitalmesh_agents.briefer import Briefer
from vitalmesh_agents.checker import Checker
from vitalmesh_agents.checker_handler import make_checker_handler
from vitalmesh_agents.guardian_handler import make_guardian_handler
from vitalmesh_agents.release import ReleaseGate
from vitalmesh_agents.release_handler import make_release_handler
from vitalmesh_agents.runstore import RedisRunStore
from vitalmesh_agents.specialist_handler import SpecialistDeps, make_specialist_handler
from vitalmesh_agents.synthesizer import Synthesizer
from vitalmesh_agents.synthesizer_handler import make_synthesizer_handler
from vitalmesh_agents.triage_handler import make_triage_handler
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.kafka import (
    KafkaPublisher,
    KafkaSettings,
    SchemaRegistrySerde,
    ensure_topics,
)
from vitalmesh_common.kafka_testing import fetch_through, only_for, running_consumers
from vitalmesh_common.processor import Handler
from vitalmesh_common.run import Specialist
from vitalmesh_common.topics import (
    ADVICE_BUNDLES,
    ADVICE_COMPLETED,
    DRAFTS_CLEARED,
    QUESTIONS_ASKED,
    RECOMMENDATIONS_DRAFTED,
    RECOMMENDATIONS_RELEASED,
    REVIEWS_COMPLETED,
    REVIEWS_DECIDED,
    REVIEWS_REQUESTED,
    RUNS_PLANNED,
)
from vitalmesh_evidence.types import EvidenceAnswer
from vitalmesh_llm import LLMRequest, LLMResponse, ToolCall
from vitalmesh_stores import (
    InMemoryHistoryStore,
    ProfileRecord,
    RedisLabStore,
    RedisProfileStore,
)
from vitalmesh_wellness.engine import Profile

pytestmark = pytest.mark.integration

DSN = "postgresql://vitalmesh@127.0.0.1:5432/vitalmesh"
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
        prompt_tokens=1,
        completion_tokens=1,
        latency_ms=1.0,
        finish_reason="tool_calls" if calls else "stop",
        tool_calls=calls,
    )


class ScriptedRouter:
    """Answers by purpose: tool first for each specialist, then a checked, number-free text."""

    def __init__(self) -> None:
        self.purposes: list[str] = []

    def complete(self, req: LLMRequest, max_wait_s: float = 0.0) -> LLMResponse:
        self.purposes.append(req.purpose)
        if req.purpose in TOOL_FIRST:
            if any(m.role == "tool" for m in req.messages):
                return reply("This follows your own targets.")
            return reply("", (ToolCall(id="c0", name=TOOL_FIRST[req.purpose], arguments="{}"),))
        if req.purpose == "synthesizer":
            return reply("Here is a short plan that follows your own targets.")
        if req.purpose == "checker":
            return reply(json.dumps({"verdict": "approve", "problems": []}))
        if req.purpose == "briefer":
            return reply("The user asked for a workout and the system flagged it for review.")
        raise AssertionError(f"unexpected purpose {req.purpose}")


class NoEvidence:
    def answer(self, question: str) -> EvidenceAnswer:
        return EvidenceAnswer(status="insufficient_evidence")


@pytest.fixture(scope="module")
def serde(kafka_settings: KafkaSettings) -> SchemaRegistrySerde:
    ensure_topics(kafka_settings)
    return SchemaRegistrySerde(kafka_settings)


@pytest.fixture()
def publisher(kafka_settings: KafkaSettings) -> KafkaPublisher:
    return KafkaPublisher(kafka_settings, client_id="t3-it")


def seed(profiles: RedisProfileStore, run: str, conditions: list[str]) -> str:
    uid = f"t3-{run}"
    profile = Profile.model_validate(
        {
            "user_id": uid,
            "sex": "female",
            "age_years": 30,
            "weight_kg": 65,
            "height_cm": 165,
            "activity": "moderate",
            "resting_hr_bpm": 60,
            "conditions": conditions,
        }
    )
    profiles.put(ProfileRecord(profile=profile, recent_sleep_hours=tuple([8.0] * 14)))
    return uid


class Stack:
    """Every agent consuming its topics, each in its own consumer group."""

    def __init__(
        self,
        settings: KafkaSettings,
        serde: SchemaRegistrySerde,
        publisher: KafkaPublisher,
        client: redis.Redis,
        uid: str,
        run: str,
        specialists: set[Specialist],
        run_deadline_s: float = 60.0,
    ) -> None:
        self.router = ScriptedRouter()
        profiles = RedisProfileStore(client, prefix=f"it-{run}:profile")
        labs = RedisLabStore(client, prefix=f"it-{run}:labs")
        deps = SpecialistDeps(
            router=self.router,
            evidence=NoEvidence(),
            profiles=profiles,
            labs=labs,
            history=InMemoryHistoryStore(),
            sleep=lambda _s: None,
        )
        self.aggregator = Aggregator(
            RedisRunStore(client, prefix=f"it-{run}:agg"), serde, publisher
        )
        self.saver_cm = PostgresSaver.from_conn_string(DSN)
        saver = self.saver_cm.__enter__()
        saver.setup()
        gate = ReleaseGate(profiles, Briefer(self.router), serde, publisher, saver)
        lock = threading.Lock()

        def serial(h: Handler) -> Handler:
            def run_locked(meta: EventMeta, payload: Any) -> None:
                with lock:
                    h(meta, payload)

            return run_locked

        synth = Synthesizer(self.router)
        self.wiring: list[tuple[str, Handler]] = [
            (
                QUESTIONS_ASKED,
                make_triage_handler(
                    profiles, labs, serde, publisher, run_deadline_s=run_deadline_s
                ),
            ),
            (RUNS_PLANNED, serial(self.aggregator.handle)),
            (ADVICE_COMPLETED, serial(self.aggregator.handle)),
            (ADVICE_BUNDLES, make_synthesizer_handler(synth, serde, publisher)),
            (REVIEWS_COMPLETED, make_synthesizer_handler(synth, serde, publisher)),
            (RECOMMENDATIONS_DRAFTED, make_guardian_handler(profiles, serde, publisher)),
            (
                DRAFTS_CLEARED,
                make_checker_handler(Checker(self.router), profiles, serde, publisher),
            ),
            (DRAFTS_CLEARED, serial(make_release_handler(gate))),
            (REVIEWS_COMPLETED, serial(make_release_handler(gate))),
            (REVIEWS_DECIDED, serial(make_release_handler(gate))),
        ]
        for kind in specialists:
            self.wiring.append(
                (RUNS_PLANNED, make_specialist_handler(kind, deps, serde, publisher))
            )
        self.uid = uid
        self.stop = threading.Event()

    @contextlib.contextmanager
    def running(
        self,
        settings: KafkaSettings,
        serde: SchemaRegistrySerde,
        publisher: KafkaPublisher,
        client: redis.Redis,
    ) -> Iterator[None]:
        def sweep() -> None:
            while not self.stop.wait(0.5):
                self.aggregator.sweep()

        sweeper = threading.Thread(target=sweep, daemon=True)
        with contextlib.ExitStack() as stack:
            for topic, handler in self.wiring:
                stack.enter_context(
                    running_consumers(
                        settings, serde, publisher, client, topic, only_for(self.uid, handler)
                    )
                )
            sweeper.start()
            try:
                yield
            finally:
                self.stop.set()
                sweeper.join(timeout=5)
                self.saver_cm.__exit__(None, None, None)


def ask(serde: SchemaRegistrySerde, publisher: KafkaPublisher, uid: str, text: str) -> str:
    corr = f"corr-{uuid.uuid4().hex[:8]}"
    meta = EventMeta(
        correlation_id=corr,
        user_id=uid,
        household_id="h-it",
        type="question.asked",
        occurred_at=datetime.now(UTC),
        trace_id=f"trace-{corr}",
    )
    publisher.publish(
        QUESTIONS_ASKED,
        uid,
        serde.serialize_event(
            QUESTIONS_ASKED, meta, payloads_pb2.QuestionAsked(question=text, session_id="s")
        ),
    )
    publisher.flush()
    return corr


def wait_event(
    settings: KafkaSettings,
    serde: SchemaRegistrySerde,
    topic: str,
    corr: str,
    timeout: float = 90.0,
) -> tuple[EventMeta, Any]:
    def decode(data: bytes) -> tuple[EventMeta, Any]:
        return serde.deserialize_event(topic, data)

    got = fetch_through(
        settings, topic, decode, lambda item: item[0].correlation_id == corr, timeout=timeout
    )
    assert got and got[-1][0].correlation_id == corr, f"no event on {topic} for {corr}"
    result: tuple[EventMeta, Any] = got[-1]
    return result


def test_a_low_risk_question_runs_the_whole_graph_and_is_released(
    kafka_settings: KafkaSettings,
    serde: SchemaRegistrySerde,
    publisher: KafkaPublisher,
    redis_client: redis.Redis,
) -> None:
    run = uuid.uuid4().hex[:8]
    uid = seed(RedisProfileStore(redis_client, prefix=f"it-{run}:profile"), run, [])
    stack = Stack(
        kafka_settings,
        serde,
        publisher,
        redis_client,
        uid,
        run,
        {Specialist.NUTRITION, Specialist.MOVEMENT_RECOVERY},
    )
    with stack.running(kafka_settings, serde, publisher, redis_client):
        corr = ask(serde, publisher, uid, "What should I eat and how should I train?")
        meta, released = wait_event(kafka_settings, serde, RECOMMENDATIONS_RELEASED, corr)
    assert meta.type == "recommendation.released" and meta.tier is not None
    assert meta.correlation_id == corr and meta.trace_id == f"trace-{corr}"
    assert released.summary.startswith("Here is a short plan")
    assert not released.partial and len(released.claims) >= 2
    assert released.rules_version.endswith("+t3-1")
    # low risk: no Checker, no human
    assert "checker" not in stack.router.purposes and "briefer" not in stack.router.purposes
    assert stack.router.purposes.count("synthesizer") == 1


def test_a_missing_specialist_does_not_stall_the_run_it_is_released_partial(
    kafka_settings: KafkaSettings,
    serde: SchemaRegistrySerde,
    publisher: KafkaPublisher,
    redis_client: redis.Redis,
) -> None:
    run = uuid.uuid4().hex[:8]
    uid = seed(RedisProfileStore(redis_client, prefix=f"it-{run}:profile"), run, [])
    stack = Stack(
        kafka_settings,
        serde,
        publisher,
        redis_client,
        uid,
        run,
        {Specialist.NUTRITION},  # the movement specialist is "dead"
        run_deadline_s=4.0,
    )
    with stack.running(kafka_settings, serde, publisher, redis_client):
        corr = ask(serde, publisher, uid, "What should I eat and how should I train?")
        _bundle_meta, bundle = wait_event(kafka_settings, serde, ADVICE_BUNDLES, corr)
        _, released = wait_event(kafka_settings, serde, RECOMMENDATIONS_RELEASED, corr)
    assert bundle.reason == "deadline" and bundle.partial
    assert len(bundle.missing) == 1
    assert released.partial and "partial" in released.summary.lower()
    assert "movement recovery" in released.summary


def test_a_high_risk_question_waits_for_a_human_then_releases_on_approval(
    kafka_settings: KafkaSettings,
    serde: SchemaRegistrySerde,
    publisher: KafkaPublisher,
    redis_client: redis.Redis,
) -> None:
    run = uuid.uuid4().hex[:8]
    uid = seed(RedisProfileStore(redis_client, prefix=f"it-{run}:profile"), run, ["cardiac"])
    stack = Stack(
        kafka_settings,
        serde,
        publisher,
        redis_client,
        uid,
        run,
        set(Specialist),
    )
    with stack.running(kafka_settings, serde, publisher, redis_client):
        corr = ask(serde, publisher, uid, "Give me a hard workout")
        _, request = wait_event(kafka_settings, serde, REVIEWS_REQUESTED, corr)
        assert list(request.reasons) == ["risk.high"] and request.briefing
        assert "checker" in stack.router.purposes  # the Checker saw it first
        decision = EventMeta(
            correlation_id=corr,
            user_id=uid,
            household_id="h-it",
            type="review.decided",
            occurred_at=datetime.now(UTC),
        )
        publisher.publish(
            REVIEWS_DECIDED,
            uid,
            serde.serialize_event(
                REVIEWS_DECIDED,
                decision,
                payloads_pb2.ReviewDecided(
                    recommendation_id=request.recommendation_id,
                    decision=payloads_pb2.DECISION_APPROVE,
                    reviewer_id="clinician-it",
                ),
            ),
        )
        publisher.flush()
        meta, released = wait_event(kafka_settings, serde, RECOMMENDATIONS_RELEASED, corr)
    assert released.recommendation_id == request.recommendation_id
    assert any(c.metric == "max_hr_zone" for c in released.claims)  # the condition review ran
    assert meta.correlation_id == corr
