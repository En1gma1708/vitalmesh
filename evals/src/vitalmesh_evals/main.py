"""CLI: ``uv run python -m vitalmesh_evals [--explainer template|llm] [--only CATEGORY ...]``.

``template`` runs everything with no model (the baseline, free). ``llm`` sends the explainer
cases to the router (Groq first, paced to its token budget); deterministic answers are cached in
Redis, so a repeat run spends no quota. Reports are written to ``evals/results/`` (gitignored).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import redis
from vitalmesh_llm import RedisCache, Router, load_keys

from vitalmesh_evals.cases import all_cases
from vitalmesh_evals.runner import code_fingerprint, run_cases, save_report
from vitalmesh_evals.suts import (
    SYSTEM_PROMPT,
    ExplainSut,
    LLMExplainer,
    Sut,
    T0Sut,
    TemplateExplainer,
)

RESULTS = Path(__file__).resolve().parents[2] / "results"


def main() -> None:
    ap = argparse.ArgumentParser(prog="vitalmesh_evals")
    ap.add_argument("--explainer", choices=["template", "llm"], default="template")
    ap.add_argument("--only", nargs="*", help="categories to run (default: all)")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="ignore the checkpoint, rerun all")
    args = ap.parse_args()

    cases = [c for c in all_cases() if not args.only or c.category in args.only]
    if args.explainer == "llm":
        client = redis.Redis.from_url("redis://127.0.0.1:6379/0")
        router = Router(load_keys(Path(".env")), cache=RedisCache(client))
        explain: Sut = ExplainSut(LLMExplainer(router))
    else:
        explain = ExplainSut(TemplateExplainer())
    checkpoint = RESULTS / f"checkpoint-{args.explainer}.jsonl"
    if args.fresh and checkpoint.exists():
        checkpoint.unlink()
    report = run_cases(
        args.explainer,
        cases,
        {"t0": T0Sut(), "explain": explain},
        checkpoint=checkpoint,
        config=f"{args.explainer}|{SYSTEM_PROMPT}|{code_fingerprint()}",
        progress=print,
    )
    print(report.summary())
    if not args.no_save:
        print("saved", save_report(report, RESULTS))


if __name__ == "__main__":
    main()
