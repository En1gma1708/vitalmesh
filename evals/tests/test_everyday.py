"""The everyday-question eval is a gate (D108): the tuning set stays at 100 percent and the
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


def test_the_case_set_matches_the_recorded_baseline_sizes() -> None:
    tuning = [c for c in CASES if c.group == "tuning"]
    held = [c for c in CASES if c.group == "heldout"]
    assert len(tuning) == BASELINE["tuning"]["cases"]
    assert len(held) == BASELINE["heldout"]["cases"]
    assert len({c.id for c in CASES}) == len(CASES)


def test_the_tuning_set_passes_completely_and_the_held_out_set_has_not_regressed() -> None:
    results = run_all()
    failed = [(r.case.id, r.problems) for r in results if r.case.group == "tuning" and not r.passed]
    assert not failed, failed
    s = summary(results)
    assert s["tuning"]["passed"] == s["tuning"]["cases"]
    assert s["heldout"]["passed"] >= BASELINE["heldout"]["passed"], s["heldout"]
