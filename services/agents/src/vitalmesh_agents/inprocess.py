"""The T3 graph on an in-process bus: the REAL handlers, a synchronous queue instead of Kafka (D68).

Why it exists: the ablation (single agent vs one specialist vs the full graph) and counterfactual
replay (the same recorded questions through a new prompt or model) both need many runs that are
deterministic, free of broker timing and cheap to repeat. Every handler is the one the Kafka
services run; only the transport differs, so a result here is a result about the agents, not
about the bus. It says nothing about throughput, ordering across partitions or redelivery: those
are tested against the real stack (``tests/integration/test_t3_chain.py``).

Delivery is FIFO and exceptions are recorded, not retried; the human step is not simulated: a run
that asks for a human review ends in ``review_requested``.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_common.envelope import EventMeta, new_id, utcnow
from vitalmesh_common.processor import Handler
from vitalmesh_common.run import Specialist
from vitalmesh_common.testing import PlainSerde
from vitalmesh_common.topics import (
    ADVICE_BUNDLES,
    ADVICE_COMPLETED,
    DRAFTS_CLEARED,
    QUESTIONS_ASKED,
    RECOMMENDATIONS_DRAFTED,
    RECOMMENDATIONS_RELEASED,
    REVIEWS_COMPLETED,
    REVIEWS_DECIDED,
    RUNS_PLANNED,
)
from vitalmesh_llm.types import Alias
from vitalmesh_stores import ProfileStore

from vitalmesh_agents.aggregator import Aggregator
from vitalmesh_agents.briefer import Briefer
from vitalmesh_agents.checker import Checker
from vitalmesh_agents.checker_handler import make_checker_handler
from vitalmesh_agents.cohort_handler import make_cohort_handler, make_fill_handler
from vitalmesh_agents.cohortstore import CohortStore
from vitalmesh_agents.guardian_handler import make_guardian_handler
from vitalmesh_agents.llmcall import RouterLike
from vitalmesh_agents.release import ReleaseGate
from vitalmesh_agents.release_handler import make_release_handler
from vitalmesh_agents.runstore import InMemoryRunStore
from vitalmesh_agents.specialist_handler import SpecialistDeps, make_specialist_handler
from vitalmesh_agents.synthesizer import Synthesizer
from vitalmesh_agents.synthesizer_handler import make_synthesizer_handler
from vitalmesh_agents.triage import Load
from vitalmesh_agents.triage_handler import make_triage_handler

MAX_STEPS = 400


@dataclass
class Seen:
    topic: str
    meta: EventMeta
    payload: Any


@dataclass
class Bus:
    """A synchronous stand-in for Kafka that also records every event (the publisher side)."""

    serde: PlainSerde = field(default_factory=PlainSerde)
    subs: dict[str, list[Handler]] = field(default_factory=lambda: defaultdict(list))
    queue: deque[tuple[str, bytes]] = field(default_factory=deque)
    seen: list[Seen] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    sweep: Callable[[], int] = lambda: 0  # the Aggregator's deadline sweep, set by build_graph

    def subscribe(self, topic: str, handler: Handler) -> None:
        self.subs[topic].append(handler)

    def publish(
        self, topic: str, key: str, value: bytes, headers: dict[str, str] | None = None
    ) -> None:
        self.queue.append((topic, value))

    def flush(self) -> None:
        return None

    def drain(self, max_steps: int = MAX_STEPS) -> None:
        steps = 0
        while self.queue:
            steps += 1
            if steps > max_steps:
                self.errors.append(f"stopped after {max_steps} events: a loop?")
                self.queue.clear()
                return
            topic, value = self.queue.popleft()
            meta, payload = self.serde.deserialize_event(topic, value)
            self.seen.append(Seen(topic, meta, payload))
            for handler in list(self.subs.get(topic, ())):
                try:
                    handler(meta, payload)
                except Exception as exc:  # the real processor would retry; here it is a finding
                    self.errors.append(f"{topic}: {type(exc).__name__}: {str(exc)[:200]}")

    def of_type(self, type_: str) -> list[Seen]:
        return [s for s in self.seen if s.meta.type == type_]


@dataclass(frozen=True)
class Wiring:
    router: RouterLike
    deps: SpecialistDeps
    profiles: ProfileStore
    synth_alias: Alias = "fast"
    checker_alias: Alias = "strong"
    clock: Callable[[], datetime] = utcnow
    load: Load = Load.NORMAL
    run_deadline_s: float = 90.0
    specialists: frozenset[Specialist] = frozenset(Specialist)
    cohort: CohortStore | None = None  # the T1 cache; None = every run is a full run


def build_graph(wiring: Wiring) -> Bus:
    """Every agent subscribed to its topics, exactly as ``vitalmesh_agents.main`` does."""
    bus = Bus()
    serde = bus.serde
    w = wiring
    labs = w.deps.labs
    aggregator = Aggregator(InMemoryRunStore(), serde, bus, w.clock)
    synth = Synthesizer(w.router, alias=w.synth_alias, tracer=w.deps.tracer, sleep=w.deps.sleep)
    gate = ReleaseGate(
        w.profiles,
        Briefer(w.router, tracer=w.deps.tracer, sleep=w.deps.sleep),
        serde,
        bus,
        InMemorySaver(),
        clock=w.clock,
    )
    checker = Checker(w.router, alias=w.checker_alias, tracer=w.deps.tracer, sleep=w.deps.sleep)
    bus.subscribe(
        QUESTIONS_ASKED,
        make_triage_handler(
            w.profiles,
            labs,
            serde,
            bus,
            load=lambda: w.load,
            clock=w.clock,
            run_deadline_s=w.run_deadline_s,
            cohort=w.cohort,
        ),
    )
    if w.cohort is not None:
        bus.subscribe(RUNS_PLANNED, make_cohort_handler(w.deps, w.cohort, serde, bus, w.clock))
        bus.subscribe(RECOMMENDATIONS_RELEASED, make_fill_handler(w.deps, w.cohort))
    for kind in w.specialists:
        bus.subscribe(RUNS_PLANNED, make_specialist_handler(kind, w.deps, serde, bus, w.clock))
    bus.subscribe(RUNS_PLANNED, aggregator.handle)
    bus.subscribe(ADVICE_COMPLETED, aggregator.handle)
    bus.subscribe(ADVICE_BUNDLES, make_synthesizer_handler(synth, serde, bus, w.clock))
    bus.subscribe(REVIEWS_COMPLETED, make_synthesizer_handler(synth, serde, bus, w.clock))
    bus.subscribe(
        RECOMMENDATIONS_DRAFTED, make_guardian_handler(w.profiles, serde, bus, None, w.clock)
    )
    bus.subscribe(DRAFTS_CLEARED, make_checker_handler(checker, w.profiles, serde, bus, w.clock))
    release = make_release_handler(gate)
    for topic in (DRAFTS_CLEARED, REVIEWS_COMPLETED, REVIEWS_DECIDED):
        bus.subscribe(topic, release)
    bus.sweep = aggregator.sweep
    return bus


@dataclass(frozen=True)
class Outcome:
    correlation_id: str
    final: str  # released | blocked | review_requested | none
    text: str
    events: tuple[Seen, ...]
    errors: tuple[str, ...]


def outcome_of(bus: Bus, corr: str) -> Outcome:
    mine = tuple(s for s in bus.seen if s.meta.correlation_id == corr)
    final, text = "none", ""
    for s in mine:
        if s.meta.type == "recommendation.released":
            final, text = "released", s.payload.summary
        elif s.meta.type == "recommendation.blocked" and final != "released":
            final = "blocked"
        elif s.meta.type == "review.requested" and final == "none":
            final, text = "review_requested", s.payload.draft.text
    return Outcome(corr, final, text, mine, tuple(bus.errors))


def ask(bus: Bus, user_id: str, question: str, now: datetime | None = None) -> Outcome:
    corr = f"ip-{new_id()[:8]}"
    meta = EventMeta(
        correlation_id=corr,
        user_id=user_id,
        household_id="h-eval",
        type="question.asked",
        occurred_at=now or utcnow(),
        trace_id=corr,
    )
    bus.publish(
        QUESTIONS_ASKED,
        user_id,
        bus.serde.serialize_event(
            QUESTIONS_ASKED, meta, payloads_pb2.QuestionAsked(question=question, session_id="eval")
        ),
    )
    bus.drain()
    return outcome_of(bus, corr)
