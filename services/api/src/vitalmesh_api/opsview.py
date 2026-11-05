"""The operator's view of runs (D84): flow, timing and tiers, never the content.

An operator needs to see HOW a run went (which tier, which helpers, how long each hop took, whether
a model was called), not WHAT the person asked or was told. So everything here is derived from the
run log entries with the question text, the answer text, the claims and the checker's free-text
problems removed (only counts remain). The numbers are measured from event times (each event
carries the time its producer stamped it), over the runs still in the run log (one hour), so a
percentile here is over a small, stated sample and says how many runs it covers.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from vitalmesh_api.runview import status_of

_FIRST = "question.asked"
_LAST = ("recommendation.released", "recommendation.blocked")
_DROP = {"question", "text", "claims", "citations", "problems"}


class LagGroup(BaseModel):
    group: str  # one agent role: it has one consumer group per topic it reads
    lag: int  # messages published to its topics and not yet processed
    topics: list[str]  # the topics it reads (retry tiers folded into their topic)
    consumers_down: int  # how many of its consumer groups have no live consumer


class EvalRow(BaseModel):
    name: str
    passed: int
    total: int


class Hop(BaseModel):
    type: str
    at_ms: int  # milliseconds after the question was asked


class RunSummary(BaseModel):
    correlation_id: str
    started_at: str
    status: str  # running | released | needs_review | blocked
    tier: str | None
    specialists: list[str]
    risk: str | None
    degraded: bool
    partial: bool
    model_calls: int
    total_ms: int | None  # question asked -> answer released or blocked; None while running
    hops: list[Hop]


class Percentiles(BaseModel):
    runs: int  # how many finished runs the numbers cover
    p50_ms: int | None
    p95_ms: int | None


def _ms(a: str, b: str) -> int:
    return round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() * 1000)


def summarise_run(correlation_id: str, entries: list[dict[str, Any]]) -> RunSummary:
    first = entries[0]["at"] if entries else ""
    planned = next((e for e in entries if e["type"] == "run.planned"), {})
    released = next((e for e in entries if e["type"] == "recommendation.released"), {})
    last = next((e for e in entries if e["type"] in _LAST), None)
    return RunSummary(
        correlation_id=correlation_id,
        started_at=first,
        status=status_of(entries),
        tier=planned.get("tier"),
        specialists=list(planned.get("specialists", [])),
        risk=planned.get("risk"),
        degraded=bool(planned.get("degraded", False)),
        partial=bool(released.get("partial", False)),
        model_calls=sum(int(e.get("model_calls", 0)) for e in entries),
        total_ms=_ms(first, last["at"]) if last is not None and first else None,
        hops=[Hop(type=e["type"], at_ms=_ms(first, e["at"])) for e in entries if first],
    )


def percentiles(values: list[int]) -> Percentiles:
    """Nearest-rank percentiles; with few runs these are coarse, which is why ``runs`` is shown."""
    if not values:
        return Percentiles(runs=0, p50_ms=None, p95_ms=None)
    ordered = sorted(values)

    def rank(p: float) -> int:
        return ordered[max(1, math.ceil(p * len(ordered))) - 1]

    return Percentiles(runs=len(ordered), p50_ms=rank(0.5), p95_ms=rank(0.95))


def redact(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run entries for the operator: flow and internals, minus anything the person wrote or read."""
    out: list[dict[str, Any]] = []
    for e in entries:
        kept = {k: v for k, v in e.items() if k not in _DROP}
        for k in ("claims", "citations", "problems"):
            if k in e:
                kept[f"{k}_n"] = len(e[k])
        out.append(kept)
    return out


def eval_rows(directory: Path) -> list[EvalRow]:
    """Pass counts of the committed eval baselines (``{"cases": {id: passed}}`` per suite)."""
    rows: list[EvalRow] = []
    for path in sorted(directory.glob("*.json")):
        try:
            cases = json.loads(path.read_text(encoding="utf-8")).get("cases", {})
        except (OSError, ValueError):
            continue  # an unreadable baseline is omitted, not reported as a pass
        if isinstance(cases, dict) and cases:
            rows.append(
                EvalRow(
                    name=path.stem,
                    passed=sum(1 for v in cases.values() if v is True),
                    total=len(cases),
                )
            )
    return rows
