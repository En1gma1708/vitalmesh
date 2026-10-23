"""Entry point: ``uv run python -m vitalmesh_agents <role> [<role> ...]`` (D68).

Each role is its own consumer group (``agent-<role>``), so it scales and fails on its own lag.
Several roles may share one process for local demos; in production each is a deployment.

======================  ===============================  ==================================
role                    consumes                         publishes
======================  ===============================  ==================================
triage                  questions.asked                  runs.planned
nutrition / movement /  runs.planned                     advice.completed
condition
aggregator              runs.planned, advice.completed   advice.bundles (and on a timer)
synthesizer             advice.bundles, reviews.completed recommendations.drafted
guardian                recommendations.drafted          drafts.cleared | safety.blocked
checker                 drafts.cleared                   reviews.completed
release                 drafts.cleared, reviews.*        recommendations.released | blocked,
                                                         reviews.requested
======================  ===============================  ==================================
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

import redis
from vitalmesh_common.idempotency import RedisIdempotencyStore
from vitalmesh_common.kafka import (
    KafkaPublisher,
    KafkaSettings,
    SchemaRegistrySerde,
    build_consumer,
    ensure_topics,
    run_consumer,
)
from vitalmesh_common.processor import EventProcessor, Handler
from vitalmesh_common.retry import RetryPolicy
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
    RUNS_PLANNED,
    retry_topic,
)
from vitalmesh_llm import Router
from vitalmesh_llm.cache import RedisCache
from vitalmesh_llm.providers import load_keys
from vitalmesh_stores import RedisHistoryStore, RedisLabStore, RedisProfileStore
from vitalmesh_tracing import tracer_from_env

from vitalmesh_agents.aggregator import Aggregator
from vitalmesh_agents.briefer import Briefer
from vitalmesh_agents.budget import RedisLoadMonitor, RedisTierCounter
from vitalmesh_agents.checker import Checker
from vitalmesh_agents.checker_handler import make_checker_handler
from vitalmesh_agents.cohort_handler import DisabledEvidence, make_cohort_handler, make_fill_handler
from vitalmesh_agents.cohortstore import RedisCohortStore
from vitalmesh_agents.guardian_handler import make_guardian_handler
from vitalmesh_agents.release import ReleaseGate
from vitalmesh_agents.release_handler import make_release_handler
from vitalmesh_agents.runstore import RedisRunStore
from vitalmesh_agents.specialist_handler import SpecialistDeps, make_specialist_handler
from vitalmesh_agents.synthesizer import Synthesizer
from vitalmesh_agents.synthesizer_handler import make_synthesizer_handler
from vitalmesh_agents.triage_handler import make_triage_handler

log = logging.getLogger("vitalmesh.agents")
DB_DSN = os.environ.get("VITALMESH_PG_DSN", "postgresql://vitalmesh@127.0.0.1:5432/vitalmesh")
SWEEP_INTERVAL_S = 1.0

INPUTS: dict[str, tuple[str, ...]] = {
    "triage": (QUESTIONS_ASKED,),
    "nutrition": (RUNS_PLANNED,),
    "movement": (RUNS_PLANNED,),
    "condition": (RUNS_PLANNED,),
    "aggregator": (RUNS_PLANNED, ADVICE_COMPLETED),
    "synthesizer": (ADVICE_BUNDLES, REVIEWS_COMPLETED),
    "guardian": (RECOMMENDATIONS_DRAFTED,),
    "checker": (DRAFTS_CLEARED,),
    "release": (DRAFTS_CLEARED, REVIEWS_COMPLETED, REVIEWS_DECIDED),
    "cohort": (RUNS_PLANNED,),
    "cachefill": (RECOMMENDATIONS_RELEASED,),
}
_SPECIALISTS = {
    "nutrition": Specialist.NUTRITION,
    "movement": Specialist.MOVEMENT_RECOVERY,
    "condition": Specialist.CONDITION_MEDICATION,
}


def input_topics(role: str, policy: RetryPolicy) -> list[str]:
    topics: list[str] = []
    for base in INPUTS[role]:
        topics += [base, *[retry_topic(base, t) for t in range(1, policy.max_retries + 1)]]
    return topics


def _locked(handler: Handler, lock: threading.Lock) -> Handler:
    """Roles that read several topics share state across consumer threads: serialise them."""

    def run(meta: Any, payload: Any) -> None:
        with lock:
            handler(meta, payload)

    return run


class Runtime:
    def __init__(self, roles: list[str]) -> None:
        self.settings = KafkaSettings.from_env()
        self.policy = RetryPolicy()
        self.redis = redis.Redis.from_url(
            os.environ.get("VITALMESH_REDIS_URL", "redis://127.0.0.1:6379/0")
        )
        self.serde = SchemaRegistrySerde(self.settings)
        self.publisher = KafkaPublisher(self.settings, client_id="agents")
        self.tracer = tracer_from_env()
        self.stack = contextlib.ExitStack()
        self.sweepers: list[Callable[[], int]] = []
        self._router: Router | None = None
        self.roles = roles

    @property
    def router(self) -> Router:
        if self._router is None:
            self._router = Router(
                load_keys(Path(".env")), cache=RedisCache(self.redis), tracer=self.tracer
            )
        return self._router

    def handler(self, role: str) -> Handler:
        profiles = RedisProfileStore(self.redis)
        if role == "triage":
            # Triage may only plan T1 when the cohort role is running: opt in explicitly.
            use_cohort = os.environ.get("VITALMESH_COHORT_CACHE", "") == "1"
            return make_triage_handler(
                profiles,
                RedisLabStore(self.redis),
                self.serde,
                self.publisher,
                cohort=RedisCohortStore(self.redis) if use_cohort else None,
                monitor=RedisLoadMonitor(self.redis) if use_cohort else None,
                tiers=RedisTierCounter(self.redis),
            )
        if role in ("cohort", "cachefill"):
            deps = SpecialistDeps(
                router=self.router,  # never called: the T1 roles use deterministic tools only
                evidence=DisabledEvidence(),
                profiles=profiles,
                labs=RedisLabStore(self.redis),
                history=RedisHistoryStore(self.redis),
                tracer=self.tracer,
            )
            store = RedisCohortStore(self.redis)
            if role == "cohort":
                return make_cohort_handler(deps, store, self.serde, self.publisher)
            return make_fill_handler(deps, store)
        if role in _SPECIALISTS:
            return make_specialist_handler(
                _SPECIALISTS[role], self._specialist_deps(profiles), self.serde, self.publisher
            )
        if role == "aggregator":
            agg = Aggregator(RedisRunStore(self.redis), self.serde, self.publisher)
            self.sweepers.append(agg.sweep)
            return _locked(agg.handle, threading.Lock())
        if role == "synthesizer":
            synth = Synthesizer(self.router, tracer=self.tracer)
            return _locked(
                make_synthesizer_handler(synth, self.serde, self.publisher), threading.Lock()
            )
        if role == "guardian":
            return make_guardian_handler(profiles, self.serde, self.publisher)
        if role == "checker":
            return make_checker_handler(
                Checker(self.router, tracer=self.tracer), profiles, self.serde, self.publisher
            )
        if role == "release":
            from langgraph.checkpoint.postgres import PostgresSaver

            saver = self.stack.enter_context(PostgresSaver.from_conn_string(DB_DSN))
            saver.setup()  # idempotent: creates the checkpoint tables on first use
            gate = ReleaseGate(
                profiles,
                Briefer(self.router, tracer=self.tracer),
                self.serde,
                self.publisher,
                saver,
            )
            return _locked(make_release_handler(gate), threading.Lock())
        raise ValueError(f"unknown role {role!r}; choose from {sorted(INPUTS)}")

    def _specialist_deps(self, profiles: RedisProfileStore) -> SpecialistDeps:
        from vitalmesh_evidence import EvidenceAgent, StoreRetriever
        from vitalmesh_retrieval import Store
        from vitalmesh_retrieval.embed import LocalEmbedder

        embedder = LocalEmbedder()
        evidence = EvidenceAgent(
            self.router, StoreRetriever(Store(DB_DSN, embedder.dim), embedder), tracer=self.tracer
        )
        return SpecialistDeps(
            router=self.router,
            evidence=evidence,
            profiles=profiles,
            labs=RedisLabStore(self.redis),
            history=RedisHistoryStore(self.redis),
            tracer=self.tracer,
        )


def main(argv: list[str] | None = None) -> None:
    roles = list(argv if argv is not None else sys.argv[1:])
    if not roles:
        raise SystemExit(f"usage: python -m vitalmesh_agents <role>...  roles: {sorted(INPUTS)}")
    for role in roles:
        if role not in INPUTS:
            raise SystemExit(f"unknown role {role!r}; choose from {sorted(INPUTS)}")
    logging.basicConfig(
        level=os.environ.get("VITALMESH_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    rt = Runtime(roles)
    created = ensure_topics(rt.settings)
    if created:
        log.info("created topics: %s", ", ".join(created))

    stop = threading.Event()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        log.info("signal %s received, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    threads: list[threading.Thread] = []
    with rt.stack:
        for role in roles:
            group = f"agent-{role}"
            processor = EventProcessor(
                group=group,
                serde=rt.serde,
                publisher=rt.publisher,
                store=RedisIdempotencyStore(rt.redis),
                handler=rt.handler(role),
                policy=rt.policy,
            )
            for topic in input_topics(role, rt.policy):
                consumer = build_consumer(rt.settings, f"{group}.{topic}")
                t = threading.Thread(
                    target=run_consumer,
                    args=(consumer, [topic], processor, stop),
                    name=f"{role}-{topic}",
                    daemon=True,
                )
                t.start()
                threads.append(t)
                log.info("%s consuming %s", role, topic)
        for sweep in rt.sweepers:
            t = threading.Thread(target=_sweep_loop, args=(sweep, stop), name="sweep", daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()


def _sweep_loop(sweep: Callable[[], int], stop: threading.Event) -> None:
    while not stop.wait(SWEEP_INTERVAL_S):
        try:
            closed = sweep()
            if closed:
                log.info("deadline sweep closed %d run(s)", closed)
        except Exception:
            log.exception("deadline sweep failed; will retry")


if __name__ == "__main__":
    main()
