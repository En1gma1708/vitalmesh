"""Cache correctness sample: a T1 (cached) answer against a fresh FULL run with the real model.

    uv run python scripts/sample_cohort.py <user_id> "<question>" "<cached answer text>"

Runs the real handlers on the in-process bus (cache off, real router, real stores) for the user and
prints the fresh answer and every number in it, then checks that the numbers in the cached answer
are a subset of the numbers in the fresh run's claims. The two texts are different sentences
(a model wrote one of them), so the comparison is on numbers, not wording. Spends free-tier quota.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime

import redis
from vitalmesh_agents.advice import claim_numbers
from vitalmesh_agents.inprocess import Wiring, ask, build_graph
from vitalmesh_agents.specialist_handler import SpecialistDeps
from vitalmesh_evals.main import EVIDENCE_DSN, make_router
from vitalmesh_evidence import EvidenceAgent, StoreRetriever
from vitalmesh_retrieval import Store
from vitalmesh_retrieval.embed import LocalEmbedder
from vitalmesh_stores import RedisHistoryStore, RedisLabStore, RedisProfileStore

NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers(text: str) -> set[float]:
    return {float(m.replace(",", "")) for m in NUMBER.findall(text)}


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    user, question, cached = sys.argv[1], sys.argv[2], sys.argv[3]
    client = redis.Redis.from_url(os.environ.get("VITALMESH_REDIS_URL", "redis://127.0.0.1:6379/0"))
    router = make_router()
    embedder = LocalEmbedder()
    evidence = EvidenceAgent(router, StoreRetriever(Store(EVIDENCE_DSN, embedder.dim), embedder))
    profiles = RedisProfileStore(client)
    deps = SpecialistDeps(
        router=router,
        evidence=evidence,
        profiles=profiles,
        labs=RedisLabStore(client),
        history=RedisHistoryStore(client),
        sleep=lambda _s: None,
    )
    bus = build_graph(Wiring(router=router, deps=deps, profiles=profiles))
    out = ask(bus, user, question, datetime.now(UTC))
    print("fresh full run:", out.final, "|", out.text)
    claims = [s.payload for s in out.events if s.meta.type == "recommendation.drafted"]
    allowed = claim_numbers(list(claims[0].claims)) if claims else set()
    cached_nums, fresh_nums = numbers(cached), numbers(out.text)
    print("cached answer numbers:", sorted(cached_nums))
    print("fresh answer numbers :", sorted(fresh_nums))
    print("fresh claim numbers  :", sorted(allowed))
    missing = sorted(n for n in cached_nums if not any(abs(n - a) < 0.51 for a in allowed))
    print("cached numbers NOT supported by the fresh run's claims:", missing or "none")


if __name__ == "__main__":
    main()
