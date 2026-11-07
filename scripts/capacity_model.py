"""Capacity model for one million people (D90).

Every input carries a tag so nobody can mistake arithmetic for a measurement:

  measured    read from a run of scripts/load_pipeline.py or scripts/load_accounts.py
                (synthetic people, one laptop, time compressed; the file is named in the note)
  arithmetic  computed here from measured numbers
  assumption  a number we chose; change it and the output changes. NOT measured.

The 1M figure is arithmetic from measured rates. It is not a 1M-user test of the whole product:
10k, 100k and 1M people WERE replayed through the event pipeline (Kafka -> processor -> Redis);
the account layer, the model-backed agents and the dashboards were not run at 1M.

    uv run python scripts/capacity_model.py [--users 1000000] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Param:
    name: str
    value: float
    unit: str
    tag: str  # measured | arithmetic | assumption
    note: str


def parameters(users: int) -> dict[str, Param]:
    p: dict[str, Param] = {}

    def add(key: str, value: float, unit: str, tag: str, note: str) -> None:
        p[key] = Param(key, value, unit, tag, note)

    # ---- measured (bench/*.json, laptop with 16 cores, Docker Desktop on Windows) ----
    add(
        "proc_rate_1",
        179,
        "events/s/process",
        "measured",
        "baseline_p1: 1 processor process, saturated",
    )
    add("proc_rate_6_total", 878, "events/s", "measured", "baseline_p6: 6 processes, 30k events")
    add(
        "proc_rate_6_1m",
        786,
        "events/s",
        "measured",
        "replay_1000000: 6 processes, 1M events, 970,042 people, 0 dead-lettered",
    )
    add(
        "redis_bytes_person",
        711,
        "bytes/person",
        "measured",
        "replay_1000000: 691.2 MB for 970,042 people",
    )
    add(
        "events_per_cpu_s",
        317,
        "events/cpu-second",
        "measured",
        "replay_1000000 (339 at 30k events)",
    )
    add(
        "signins_per_core",
        12,
        "sign-ins/s/core",
        "measured",
        "D81: argon2id 12-17/s/core (low end used)",
    )
    add(
        "me_per_process",
        354,
        "requests/s/process",
        "measured",
        "D82: GET /v1/me after the speed-ups, 354-370",
    )
    # ---- assumptions (ours) ----
    add(
        "events_per_person_day",
        4,
        "events/person/day",
        "assumption",
        "wearable syncs + manual logs; not measured",
    )
    add("peak_factor", 10, "x average", "assumption", "morning peak; not measured")
    add("daily_active_share", 0.3, "share", "assumption", "people opening the app on a day")
    add(
        "requests_per_active_day",
        10,
        "requests",
        "assumption",
        "page loads and polls per active person per day",
    )
    add("signins_per_active_day", 0.3, "sign-ins", "assumption", "sessions expire; not measured")
    add(
        "questions_per_person_day",
        0.1,
        "questions",
        "assumption",
        "agent questions per person per day",
    )
    add(
        "t3_share",
        0.05,
        "share",
        "assumption",
        "questions that need the full agent graph (the rest are cached or rule-based tiers)",
    )
    add(
        "tokens_per_t3_run",
        6000,
        "tokens",
        "assumption",
        "all specialists + checker; real runs vary",
    )
    add(
        "llm_tokens_per_min_per_key",
        8000,
        "tokens/min",
        "assumption",
        "Groq free tier; a paid tier changes this by orders of magnitude",
    )
    add("vm_cores", 2, "vCPU", "assumption", "Standard_D2s_v4 class")
    add(
        "vm_price_hour",
        0.096,
        "USD/hour",
        "assumption",
        "list price of a comparable 2-vCPU VM (D2s_v3 listed ~$70/month); not billed here",
    )
    add("procs_per_vm", 2, "processes/VM", "assumption", "one process per vCPU")
    add(
        "scaling_at_6",
        786 / (6 * 179),
        "efficiency",
        "arithmetic",
        "786 / (6 x 179): 1M run against perfect scaling",
    )
    return p


def model(users: int) -> dict[str, object]:
    p = parameters(users)
    v = {k: x.value for k, x in p.items()}
    out: dict[str, tuple[float, str, str, str]] = {}

    def put(key: str, value: float, unit: str, note: str) -> None:
        out[key] = (value, unit, "arithmetic", note)

    avg_events = users * v["events_per_person_day"] / 86400
    peak_events = avg_events * v["peak_factor"]
    per_proc = v["proc_rate_6_1m"] / 6
    put("events_avg", avg_events, "events/s", "users x events/day / 86,400")
    put("events_peak", peak_events, "events/s", "average x peak factor")
    put(
        "per_process_rate",
        per_proc,
        "events/s/process",
        "1M run total / 6 (includes the shared laptop)",
    )
    procs = math.ceil(peak_events / per_proc)
    put("processors_for_peak", procs, "processes", "peak / per-process rate")
    vms = math.ceil(procs / v["procs_per_vm"])
    put(
        "vms_for_processors",
        vms,
        "VMs",
        "processes / processes per VM (Kafka and Redis are separate, below)",
    )
    put(
        "redis_gb",
        users * v["redis_bytes_person"] / 1e9,
        "GB",
        "users x bytes/person; one node holds it, a replica doubles it",
    )
    # Redis ops: 5 round trips per event (measured per-event cost model, D90)
    put(
        "redis_ops_peak",
        peak_events * 5,
        "round trips/s",
        "5 round trips per event x peak events/s; ASSUMED ~100k ops/s per Redis node "
        "(not measured): the named bottleneck past ~20k events/s",
    )
    active = users * v["daily_active_share"]
    put(
        "api_requests_peak",
        active * v["requests_per_active_day"] / 86400 * v["peak_factor"],
        "requests/s",
        "active people x requests / 86,400 x peak",
    )
    put(
        "api_processes",
        math.ceil(
            active * v["requests_per_active_day"] / 86400 * v["peak_factor"] / v["me_per_process"]
        ),
        "processes",
        "peak requests / measured /v1/me rate (an upper-cost endpoint)",
    )
    sign_peak = active * v["signins_per_active_day"] / 86400 * v["peak_factor"]
    put("signins_peak", sign_peak, "sign-ins/s", "active people x sign-ins / 86,400 x peak")
    put(
        "signin_cores",
        math.ceil(sign_peak / v["signins_per_core"]),
        "cores",
        "peak sign-ins / measured argon2 rate",
    )
    t3_runs_day = users * v["questions_per_person_day"] * v["t3_share"]
    tokens_day = t3_runs_day * v["tokens_per_t3_run"]
    put("t3_runs_day", t3_runs_day, "runs/day", "users x questions x T3 share")
    put("llm_tokens_day", tokens_day, "tokens/day", "runs x tokens per run")
    put(
        "llm_tokens_per_min_peak",
        tokens_day / 1440 * v["peak_factor"],
        "tokens/min",
        "daily / 1,440 x peak",
    )
    put(
        "free_tier_keys_needed",
        math.ceil(tokens_day / 1440 * v["peak_factor"] / v["llm_tokens_per_min_per_key"]),
        "keys",
        "peak tokens/min / free-tier cap: why a paid tier or own model is a named bottleneck",
    )
    total_vms = (
        vms + math.ceil(out["api_processes"][0] / 2) + 3
    )  # + API VMs + Kafka/Redis/Postgres nodes
    put(
        "vms_total_rough",
        total_vms,
        "VMs",
        "processor VMs + API VMs (2 per VM) + 3 for Kafka, Redis, Postgres: a rough floor",
    )
    put(
        "cost_month",
        total_vms * v["vm_price_hour"] * 730,
        "USD/month",
        "VMs x list price x 730 h; excludes LLM, storage, egress, managed services",
    )
    return {
        "params": {k: x.__dict__ for k, x in p.items()},
        "derived": {
            k: dict(zip(("value", "unit", "tag", "note"), x, strict=True)) for k, x in out.items()
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--users", type=int, default=1_000_000)
    ap.add_argument("--json")
    a = ap.parse_args()
    m = model(a.users)
    print(
        f"Capacity model for {a.users:,} people (measured on one laptop with synthetic people; "
        "the rest is arithmetic or assumption)\n"
    )
    print("INPUTS")
    for x in m["params"].values():  # type: ignore[attr-defined]
        print(f"  [{x['tag']:10}] {x['name']:26} {x['value']:>10,.3g} {x['unit']:18} {x['note']}")
    print("\nDERIVED (all arithmetic)")
    for k, x in m["derived"].items():  # type: ignore[attr-defined]
        print(f"  {k:26} {x['value']:>12,.3g} {x['unit']:16} {x['note']}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(m, f, indent=1)


if __name__ == "__main__":
    main()
