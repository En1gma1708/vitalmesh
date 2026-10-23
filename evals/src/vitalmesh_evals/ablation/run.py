"""Run the ablation: ``uv run python -m vitalmesh_evals.ablation.run [--arms ...] [--cases ...]``.

Needs Postgres with the corpus (the Evidence agent), the local embedding model and the Groq key.
Resumable: every finished (arm, case) is appended to ``evals/results/ablation-checkpoint.jsonl``
as soon as it completes, keyed by a hash of the case, the arm and the code that produced it; a
result that hit a provider outage is NOT saved (it says nothing about the arm) and reruns.
Spends free-tier quota (Groq 8,000 tokens a minute, and a daily cap): run it in the background.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from vitalmesh_evals.ablation import arms as arms_mod
from vitalmesh_evals.ablation import cases as cases_mod
from vitalmesh_evals.ablation import metrics
from vitalmesh_evals.ablation.arms import ARMS, ArmResult
from vitalmesh_evals.ablation.cases import CASES
from vitalmesh_evals.runner import _append_line

RESULTS = Path("evals/results")
CHECKPOINT = RESULTS / "ablation-checkpoint.jsonl"


def code_fingerprint() -> str:
    """Editing an arm, a case file or the agents' prompts invalidates old results."""
    from vitalmesh_agents import advice, checker, synthesizer
    from vitalmesh_specialists import condition, movement, nutrition

    h = hashlib.sha256()
    for mod in (arms_mod, cases_mod, advice, synthesizer, checker, condition, movement, nutrition):
        h.update(Path(mod.__file__ or "").read_bytes())
    return h.hexdigest()[:12]


def fp(arm: str, case_json: str, code: str) -> str:
    return hashlib.sha256(f"{arm}|{case_json}|{code}".encode()).hexdigest()[:16]


def load_checkpoint() -> dict[str, ArmResult]:
    done: dict[str, ArmResult] = {}
    if CHECKPOINT.exists():
        for line in CHECKPOINT.read_text(encoding="utf-8").splitlines():
            try:
                doc = json.loads(line)
                done[doc["fp"]] = ArmResult.model_validate(doc["result"])
            except (ValueError, KeyError):
                continue
    return done


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="vitalmesh_evals.ablation.run")
    ap.add_argument("--arms", nargs="*", default=list(ARMS), choices=list(ARMS))
    ap.add_argument("--cases", nargs="*", help="case ids (default: all)")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    chosen = [c for c in CASES if not args.cases or c.id in args.cases]
    done = {} if args.fresh else load_checkpoint()
    code = code_fingerprint()

    from vitalmesh_evidence import EvidenceAgent, StoreRetriever
    from vitalmesh_retrieval import Store
    from vitalmesh_retrieval.embed import LocalEmbedder

    from vitalmesh_evals.main import EVIDENCE_DSN, make_router

    router = make_router()
    embedder = LocalEmbedder()
    evidence = EvidenceAgent(router, StoreRetriever(Store(EVIDENCE_DSN, embedder.dim), embedder))

    scored: list[metrics.Scored] = []
    for case in chosen:
        for arm in args.arms:
            key = fp(arm, case.model_dump_json(), code)
            if key in done:
                result, tag = done[key], "resumed"
            else:
                result, tag = ARMS[arm](case, router, evidence), "ran"
                if result.infra_error:
                    tag = "PROVIDER OUTAGE (not saved)"
                elif not args.no_save:
                    _append_line(CHECKPOINT, json.dumps({"fp": key, "result": result.model_dump()}))
            s = metrics.score(result, case)
            scored.append(s)
            print(
                f"{case.id:22} {arm:6} {result.final:17} covered={s.covered!s:5} "
                f"viol={list(s.violations)} calls={result.model_calls} "
                f"tok={result.prompt_tokens + result.completion_tokens} "
                f"{result.seconds:.0f}s [{tag}]",
                flush=True,
            )
            if result.final == "released":
                print(f"    {result.text[:300]!r}", flush=True)
            elif result.gate_rules:
                print(f"    gate: {list(result.gate_rules)}", flush=True)

    usable = [s for s in scored if not s.result.infra_error]
    print("\n" + metrics.table(usable))
    outages = len(scored) - len(usable)
    if outages:
        print(f"\n{outages} run(s) hit a provider outage and are excluded; rerun to resume.")
    if not args.no_save and usable:
        RESULTS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = RESULTS / f"ablation-{stamp}.json"
        out.write_text(
            json.dumps([s.result.model_dump() for s in usable], indent=2), encoding="utf-8"
        )
        print(f"saved {out}")


if __name__ == "__main__":
    main()
