"""Committed per-case baselines and the regression check.

A baseline records, for each case id, whether it passed in a reference run. The check fails when
a case that passed in the baseline fails now (a regression) or when a case has no baseline entry
(so adding a case forces a conscious baseline update). A case that newly passes is reported, not
failed: improvements are welcome, but they should be written into the baseline deliberately.

Baselines are written only from runs with no provider errors, so an outage can never be recorded
as a result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from vitalmesh_evals.runner import Report

BASELINE_VERSION = 1
BASELINE_DIR = Path(__file__).resolve().parents[2] / "baselines"


@dataclass
class Comparison:
    regressions: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # cases with no baseline entry
    stale: list[str] = field(default_factory=list)  # baseline entries with no such case now

    @property
    def ok(self) -> bool:
        return not self.regressions and not self.missing

    def summary(self) -> str:
        parts = []
        for label, ids in (
            ("REGRESSIONS (passed before, fail now)", self.regressions),
            ("no baseline entry (rewrite the baseline)", self.missing),
            ("newly passing", self.improvements),
            ("stale baseline entries", self.stale),
        ):
            if ids:
                parts.append(f"{label}: {', '.join(sorted(ids))}")
        return "\n".join(parts) or "matches the baseline"


def path_for(name: str) -> Path:
    return BASELINE_DIR / f"{name}.json"


def write_baseline(report: Report, name: str, directory: Path | None = None) -> Path:
    errors = [r.case_id for r in report.results if r.error]
    if errors:
        raise ValueError(f"refusing to write a baseline from a run with provider errors: {errors}")
    target = (directory or BASELINE_DIR) / f"{name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "version": BASELINE_VERSION,
        "explainer": name,
        "providers": sorted({r.provider for r in report.results if r.provider}),
        "cases": {r.case_id: r.passed for r in sorted(report.results, key=lambda r: r.case_id)},
    }
    target.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return target


def load_baseline(path: Path) -> dict[str, bool]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("version") != BASELINE_VERSION:
        raise ValueError(f"unsupported baseline version in {path}")
    return {str(k): bool(v) for k, v in doc["cases"].items()}


def compare(report: Report, baseline: dict[str, bool]) -> Comparison:
    cmp = Comparison()
    seen = set()
    for r in report.results:
        seen.add(r.case_id)
        if r.case_id not in baseline:
            cmp.missing.append(r.case_id)
        elif baseline[r.case_id] and not r.passed:
            cmp.regressions.append(r.case_id)
        elif not baseline[r.case_id] and r.passed:
            cmp.improvements.append(r.case_id)
    cmp.stale = [i for i in baseline if i not in seen]
    return cmp
