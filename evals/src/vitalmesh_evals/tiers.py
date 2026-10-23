"""Tier mix and model calls avoided by the T1 cohort cache and T0 shedding (D70).

A mechanism benchmark on the in-process bus with a SCRIPTED model: the same sequence of
(user, question) asks runs twice, once with the cache off (every run is a full run) and once with
it on. It reports how the runs split across tiers, how many model calls the scripted model saw,
and a correctness check: for every cache hit, the personalised text must equal the text the
full-run arm produced for the same user and question ("staleness rate"). The scripted model
restates the energy target it is given, so the full run's text and a personalised template agree
exactly when the personalisation is right.

What this does NOT show: real token cost, latency or answer quality (the model is scripted), or
behaviour under concurrent load. The call counts are the scripted model's calls; a real run makes
about the same NUMBER of calls per specialist but they cost real tokens. Say that wherever the
numbers are quoted.

    uv run python -m vitalmesh_evals.tiers [users]
"""

from __future__ import annotations

import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass

from vitalmesh_agents.cohortstore import InMemoryCohortStore
from vitalmesh_agents.inprocess import ask
from vitalmesh_agents.testing import T0, world

QUESTIONS = ("How many calories should I eat?", "How much should I sleep?")


@dataclass(frozen=True)
class ArmResult:
    tiers: Counter[str]
    model_calls: int
    texts: dict[tuple[str, str], str]
    errors: int
    millis: dict[str, list[float]]  # wall time per run, by tier


def run_arm(users: int, cache: bool, rounds: int = 2) -> ArmResult:
    store = InMemoryCohortStore() if cache else None
    bus, router, _ = world(cohort=store, numeric=True, extra_users=users)
    tiers: Counter[str] = Counter()
    texts: dict[tuple[str, str], str] = {}
    errors = 0
    millis: dict[str, list[float]] = defaultdict(list)
    for _ in range(rounds):
        for i in range(users):
            for question in QUESTIONS:
                started = time.perf_counter()
                out = ask(bus, f"b-{i}", question, T0)
                elapsed = (time.perf_counter() - started) * 1000
                errors += len(out.errors) + (out.final != "released")
                planned = next(s for s in out.events if s.meta.type == "run.planned")
                tier = planned.meta.tier.value if planned.meta.tier else "?"
                tiers[tier] += 1
                millis[tier].append(elapsed)
                texts[(f"b-{i}", question)] = out.text
    return ArmResult(tiers, len(router.purposes), texts, errors, dict(millis))


def main(argv: list[str]) -> None:
    users = int(argv[0]) if argv else 30
    off, on = run_arm(users, cache=False), run_arm(users, cache=True)
    total = sum(on.tiers.values())
    hits = on.tiers.get("T1", 0)
    stale = sum(1 for k, v in on.texts.items() if v != off.texts[k])
    print(f"users {users}, questions {len(QUESTIONS)}, {total} runs per arm (2 rounds)")
    print(f"cache off: tiers {dict(off.tiers)}, scripted model calls {off.model_calls}")
    print(f"cache on : tiers {dict(on.tiers)}, scripted model calls {on.model_calls}")
    saved = off.model_calls - on.model_calls
    print(f"model calls avoided: {saved} ({100 * saved / off.model_calls:.0f}%)")
    print(f"cache hits: {hits} of {total} runs ({100 * hits / total:.0f}%)")
    print(f"staleness: {stale} of {len(on.texts)} answers differ from the full run's answer")
    print(f"errors: off {off.errors}, on {on.errors}")
    for name, arm in (("cache off", off), ("cache on ", on)):
        for tier, values in sorted(arm.millis.items()):
            ordered = sorted(values)
            p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
            print(
                f"{name} {tier}: n={len(values)}, p50 {statistics.median(values):.1f} ms, "
                f"p95 {p95:.1f} ms (in-process handlers, scripted model, no network)"
            )
    print("scripted model: counts the mechanism, not real tokens, latency or quality")


if __name__ == "__main__":
    main(sys.argv[1:])
