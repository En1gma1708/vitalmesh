"""A model stand-in for LOAD TESTS of the agent graph (D91). Never for people.

``SimulatedRouter`` answers like the scripted test router (each specialist calls its first tool and
then gives a number-free text, the Synthesizer writes a fixed reply, the Checker approves, the
Briefer writes one sentence) but waits ``latency_ms`` first, so the graph's own cost (Kafka hops,
Redis, Postgres, the Guardian, the Aggregator) is measured with a model that is as slow as you say
and never rate-limited. It spends no quota and makes no network call.

It must not reach real people: answers are fixed text, not advice. ``simulated_router_from_env``
therefore refuses unless the process was started with the explicit benchmark flag.
"""

from __future__ import annotations

import os
import threading
import time
from collections import Counter
from pathlib import Path

from vitalmesh_llm import AllProvidersFailed, LLMRequest, LLMResponse

from vitalmesh_agents.testing import ScriptedRouter

ALLOW_ENV = "VITALMESH_ALLOW_SIMULATED_LLM"
ALLOW_VALUE = "benchmark-only"
LATENCY_ENV = "VITALMESH_SIM_LLM_MS"
OUTAGE_FILE_ENV = "VITALMESH_SIM_LLM_OUTAGE_FILE"  # while this file exists, every call fails


class SimulatedRouter:
    def __init__(self, latency_ms: float = 0.0, outage_file: str = "") -> None:
        if latency_ms < 0:
            raise ValueError("latency_ms must not be negative")
        self.latency_ms = latency_ms
        self.outage_file = outage_file
        self.calls: Counter[str] = Counter()
        self._lock = threading.Lock()
        self._inner = ScriptedRouter()  # holds no state we read; its purpose list is cleared below

    def complete(self, req: LLMRequest, max_wait_s: float = 0.0) -> LLMResponse:
        if self.outage_file and Path(self.outage_file).exists():
            with self._lock:
                self.calls["outage"] += 1
            raise AllProvidersFailed(["simulated outage"])
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000.0)
        with self._lock:  # the scripted router appends to a list: keep it from growing forever
            resp = self._inner.complete(req, max_wait_s)
            self._inner.purposes.clear()
            self.calls[req.purpose] += 1
        return resp.model_copy(update={"provider": "simulated", "latency_ms": self.latency_ms})


def simulated_router_from_env() -> SimulatedRouter:
    if os.environ.get(ALLOW_ENV) != ALLOW_VALUE:
        raise SystemExit(
            f"refusing the simulated model: set {ALLOW_ENV}={ALLOW_VALUE} (benchmarks only). "
            "Its answers are fixed text, not advice."
        )
    return SimulatedRouter(
        float(os.environ.get(LATENCY_ENV, "0")), os.environ.get(OUTAGE_FILE_ENV, "")
    )
