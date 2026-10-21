"""CLI: ``uv run python -m vitalmesh_evals [--explainer template|llm] [--only CATEGORY ...]``.

``template`` runs everything with no model (the baseline, free). ``llm`` sends the explainer
cases to the router (Groq first, paced to its token budget); deterministic answers are cached in
Redis, so a repeat run spends no quota. Reports are written to ``evals/results/`` (gitignored).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import redis
from vitalmesh_concierge.concierge import CLASSIFY_PROMPT, COACH_PROMPT
from vitalmesh_evidence.agent import SYSTEM_PROMPT as EVIDENCE_PROMPT
from vitalmesh_llm import RedisCache, Router, load_keys
from vitalmesh_specialists.nutrition import SYSTEM_PROMPT as NUTRITION_PROMPT
from vitalmesh_specialists.tools import SPECS as _TOOL_SPECS

from vitalmesh_evals.baseline import compare, load_baseline, path_for, write_baseline
from vitalmesh_evals.case import EvalCase, g
from vitalmesh_evals.cases import all_cases
from vitalmesh_evals.guard import GuardedExplainer
from vitalmesh_evals.judge import JUDGE_SYSTEM, Judge
from vitalmesh_evals.judge_calibration import calibrate
from vitalmesh_evals.runner import code_fingerprint, run_cases, save_report
from vitalmesh_evals.suts import (
    SYSTEM_PROMPT,
    Explainer,
    ExplainSut,
    JudgedExplainer,
    LLMExplainer,
    Sut,
    T0Sut,
    TemplateExplainer,
)

RESULTS = Path(__file__).resolve().parents[2] / "results"


def make_router() -> Router:
    client = redis.Redis.from_url("redis://127.0.0.1:6379/0")
    return Router(load_keys(Path(".env")), cache=RedisCache(client))


EVIDENCE_DSN = "postgresql://vitalmesh@127.0.0.1:5432/vitalmesh"


TOOL_SPECS_FINGERPRINT = "|".join(f"{t.name}:{t.description}" for t in _TOOL_SPECS)
LIVE_CATEGORIES = ("evidence", "nutrition", "concierge")


def build_live_suts(categories: list[str]) -> dict[str, Sut]:
    from vitalmesh_concierge import Concierge
    from vitalmesh_evidence import EvidenceAgent, StoreRetriever
    from vitalmesh_retrieval import Store
    from vitalmesh_retrieval.embed import LocalEmbedder
    from vitalmesh_specialists import NutritionSpecialist

    from vitalmesh_evals.concierge_sut import ConciergeSut
    from vitalmesh_evals.evidence_sut import EvidenceSut
    from vitalmesh_evals.nutrition_sut import NutritionSut

    embedder = LocalEmbedder()
    router = make_router()
    agent = EvidenceAgent(router, StoreRetriever(Store(EVIDENCE_DSN, embedder.dim), embedder))
    out: dict[str, Sut] = {}
    if "evidence" in categories:
        out["evidence"] = EvidenceSut(agent, EVIDENCE_DSN)
    specialist = NutritionSpecialist(router)
    if "nutrition" in categories:
        out["nutrition"] = NutritionSut(specialist, agent)
    if "concierge" in categories:
        out["concierge"] = ConciergeSut(Concierge(router, specialist), agent)
    return out


def with_judge_graders(case: EvalCase) -> EvalCase:
    # D61: the judge cannot reliably tell refusing from obeying (18/22 on calibration), so it
    # grades relevance and tone on the explain cases only; injection cases are graded in code.
    if case.category != "explain":
        return case
    extra = (
        g("min_judge", metric="relevance", minimum=3),
        g("min_judge", metric="tone", minimum=3),
    )
    return case.model_copy(update={"graders": (*case.graders, *extra)})


def run_calibration() -> int:
    rows = calibrate(Judge(make_router()))
    bad = [r for r in rows if not r["ok"]]
    for r in rows:
        mark = "ok  " if r["ok"] else "MISS"
        print(
            f"{mark} {r['id']:26} expect {r['expect']:14} got rel={r['relevance']} tone={r['tone']}"
            f"  {r['reason']}"
        )
    print(f"judge calibration: {len(rows) - len(bad)}/{len(rows)} items as expected")
    return 0 if len(bad) <= 1 else 1


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        # model text can hold characters a Windows console cannot print
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="vitalmesh_evals")
    ap.add_argument("--explainer", choices=["template", "llm"], default="template")
    ap.add_argument("--only", nargs="*", help="categories to run (default: all)")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument(
        "--write-baseline",
        action="store_true",
        help="record this run as the committed baseline (no provider errors allowed)",
    )
    ap.add_argument(
        "--check-baseline",
        action="store_true",
        help="exit 1 if a case that passed in the baseline fails now",
    )
    ap.add_argument("--fresh", action="store_true", help="ignore the checkpoint, rerun all")
    ap.add_argument(
        "--guard",
        action="store_true",
        help="wrap the explainer in the deterministic guard (question screen, answer checks)",
    )
    ap.add_argument(
        "--judge",
        action="store_true",
        help="add the LLM relevance and tone judge to the explain and injection cases",
    )
    ap.add_argument(
        "--calibrate-judge",
        action="store_true",
        help="score the hand-written calibration set and exit 1 if the judge misses more than one",
    )
    args = ap.parse_args()

    if args.calibrate_judge:
        raise SystemExit(run_calibration())
    # evidence and nutrition cases need Postgres, the embedding model and a key: only when named
    live = [c for c in LIVE_CATEGORIES if args.only and c in args.only]
    cases = [
        c
        for c in all_cases()
        if (c.category in args.only if args.only else c.sut not in LIVE_CATEGORIES)
    ]
    if args.judge:
        cases = [with_judge_graders(c) for c in cases]
    label = args.explainer + ("+guard" if args.guard else "") + ("+judge" if args.judge else "")
    suts: dict[str, Sut] = {}
    config_extra = ""
    if live:
        label = "+".join(live)
        suts.update(build_live_suts(live))
        config_extra = (
            EVIDENCE_PROMPT
            + NUTRITION_PROMPT
            + TOOL_SPECS_FINGERPRINT
            + CLASSIFY_PROMPT
            + COACH_PROMPT
        )
    router = make_router() if (args.explainer == "llm" or args.judge) else None
    inner: Explainer = (
        LLMExplainer(router) if args.explainer == "llm" and router else TemplateExplainer()
    )
    if args.guard:
        inner = GuardedExplainer(inner)
    if args.judge and router:
        inner = JudgedExplainer(inner, Judge(router))
    explain: Sut = ExplainSut(inner)
    checkpoint = RESULTS / f"checkpoint-{label}.jsonl"
    if args.fresh and checkpoint.exists():
        checkpoint.unlink()
    report = run_cases(
        label,
        cases,
        {"t0": T0Sut(), "explain": explain, **suts},
        checkpoint=checkpoint,
        config=(
            f"{label}|{SYSTEM_PROMPT}|{JUDGE_SYSTEM if args.judge else ''}|{config_extra}"
            f"|{code_fingerprint()}"
        ),
        progress=print,
    )
    print(report.summary())
    if args.write_baseline:
        print("baseline written:", write_baseline(report, label))
    if args.check_baseline:
        cmp = compare(report, load_baseline(path_for(label)))
        print("baseline check:", cmp.summary())
        if not cmp.ok:
            raise SystemExit(1)
    if not args.no_save:
        print("saved", save_report(report, RESULTS))


if __name__ == "__main__":
    main()
