"""The everyday-question eval is a gate (D108): the tuning set stays at 100 percent and each
held-out set may never get worse than its recorded baseline."""

from __future__ import annotations

import json
from pathlib import Path

from vitalmesh_evals.everyday import CASES, run_all, summary

BASELINE = json.loads(
    (Path(__file__).resolve().parents[1] / "baselines" / "everyday.json").read_text(
        encoding="utf-8"
    )
)
GROUPS = ("tuning", "heldout", "heldout2")


def test_the_case_set_matches_the_recorded_baseline_sizes() -> None:
    for g in GROUPS:
        assert len([c for c in CASES if c.group == g]) == BASELINE[g]["cases"], g
    assert len({c.id for c in CASES}) == len(CASES)


def test_the_tuning_set_passes_completely_and_no_held_out_set_has_regressed() -> None:
    results = run_all()
    failed = [(r.case.id, r.problems) for r in results if r.case.group == "tuning" and not r.passed]
    assert not failed, failed
    s = summary(results)
    assert s["tuning"]["passed"] == s["tuning"]["cases"]
    for g in ("heldout", "heldout2"):
        assert s[g]["passed"] >= BASELINE[g]["passed"], (g, s[g])
