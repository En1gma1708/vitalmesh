from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from vitalmesh_evals.case import EvalCase, g
from vitalmesh_evals.cases import all_cases
from vitalmesh_evals.graders import REGISTRY, numbers_in, run_grader
from vitalmesh_evals.runner import run_cases
from vitalmesh_evals.suts import (
    Explainer,
    ExplainSut,
    SutOutput,
    T0Sut,
    TemplateExplainer,
    render_user,
)
from vitalmesh_wellness.guardian import Guardian

FACTS = [
    {
        "metric": "daily_energy_target",
        "value": 2123.9,
        "unit": "kcal",
        "text": "Aim for 2124 kcal.",
    },
    {"metric": "target_hr_zone", "value": 3.0, "unit": "zone", "text": "Train in zone 3."},
]
INP = {"facts": FACTS, "question": "q"}


def grade(name: str, text: str, **args: Any) -> bool:
    return run_grader(name, {"text": text}, INP, args).passed


# --------------------------------------------------------------------- case integrity
def test_case_set_is_the_expected_size_and_well_formed() -> None:
    cases = all_cases()
    assert len(cases) == 116
    by_cat = {c.category: 0 for c in cases}
    for c in cases:
        by_cat[c.category] += 1
        for spec in c.graders:
            assert spec.type in REGISTRY, (c.id, spec.type)
    assert by_cat == {
        "t0_numbers": 10,
        "t0_safety": 8,
        "t0_plan": 8,
        "explain": 15,
        "injection": 38,
        "evidence": 24,
        "nutrition": 13,
    }


# ------------------------------------------------------------------- grader behaviour
def test_numbers_are_parsed_with_commas_and_decimals() -> None:
    assert numbers_in("about 2,124 kcal and 7.5 h, zone 3") == [2124.0, 7.5, 3.0]


def test_numbers_present_and_no_new_numbers() -> None:
    ok = "Aim for about 2,124 kcal and train in zone 3."
    assert grade("numbers_present", ok) and grade("no_new_numbers", ok)
    assert not grade("numbers_present", "Aim for 2,124 kcal.")  # zone missing
    assert not grade("no_new_numbers", ok + " That is 35 percent protein.")  # invented
    assert not grade("numbers_present", "Aim for 2,500 kcal and zone 3.")  # wrong value


def test_forbidden_and_leak_and_length_and_mentions() -> None:
    assert not grade("forbidden_numbers", "your target is 500 kcal", values=[500])
    assert grade("forbidden_numbers", "your target is 2124 kcal", values=[500])
    assert not grade("forbidden_terms", "PWNED", terms=["pwned"])
    assert not grade(
        "no_prompt_leak", "Rule: Use ONLY the numbers", snippets=["use only the numbers"]
    )
    assert grade("max_words", "one two three", limit=3) and not grade(
        "max_words", "a b c d", limit=3
    )
    assert not grade("max_words", "", limit=3)  # an empty answer is not a pass
    assert grade("mentions_any", "ask your Doctor", terms=["doctor"])
    assert not grade("mentions_any", "just do it", terms=["doctor"])


def test_unknown_or_crashing_graders_fail_instead_of_passing() -> None:
    assert not run_grader("no_such_grader", {}, INP, {}).passed
    assert not run_grader("claim_equals", {}, INP, {}).passed  # missing args -> error -> fail


# ------------------------------------------------------------------------- runner
class Fixed:
    def __init__(self, out: SutOutput) -> None:
        self.out = out

    def run(self, case: EvalCase) -> SutOutput:
        return self.out


def test_runner_tallies_by_category_and_reports_failures() -> None:
    cases = [
        EvalCase(
            id="a", category="x", sut="explain", input=INP, graders=(g("max_words", limit=5),)
        ),
        EvalCase(
            id="b", category="x", sut="explain", input=INP, graders=(g("max_words", limit=1),)
        ),
        EvalCase(
            id="c", category="y", sut="explain", input=INP, graders=(g("max_words", limit=5),)
        ),
    ]
    rep = run_cases("t", cases, {"explain": Fixed(SutOutput({"text": "two words"}, cached=True))})
    assert rep.by_category() == {"x": (1, 2), "y": (1, 1)}
    assert rep.passed == 2 and "FAIL b" in rep.summary()


def test_a_sut_error_fails_the_case_even_if_graders_would_pass() -> None:
    case = EvalCase(
        id="a", category="x", sut="explain", input=INP, graders=(g("max_words", limit=5),)
    )
    rep = run_cases(
        "t", [case], {"explain": Fixed(SutOutput({"text": "ok"}, error="llm_unavailable"))}
    )
    assert rep.passed == 0


# --------------------------------------------------------- the cases against real code
def test_every_deterministic_case_passes_and_is_a_regression_gate() -> None:
    cases = [c for c in all_cases() if c.sut == "t0"]
    rep = run_cases("t0", cases, {"t0": T0Sut()})
    assert rep.passed == len(cases), rep.summary()


@pytest.mark.parametrize("label", ["template", "template+guard"])
def test_no_model_baselines_have_no_regressions_and_cover_every_case(label: str) -> None:
    from vitalmesh_evals.baseline import compare, load_baseline, path_for
    from vitalmesh_evals.guard import GuardedExplainer

    explainer: Explainer = TemplateExplainer()
    if label.endswith("+guard"):
        explainer = GuardedExplainer(explainer)
    cases = [c for c in all_cases() if c.sut == "explain"]
    rep = run_cases(label, cases, {"explain": ExplainSut(explainer)})
    cmp = compare(rep, load_baseline(path_for(label)))
    assert cmp.ok, cmp.summary()  # a regression, or a case nobody recorded a baseline for


def test_baseline_comparison_and_writing_rules(tmp_path: Path) -> None:
    from vitalmesh_evals.baseline import compare, load_baseline, write_baseline

    ok = Fixed(SutOutput({"text": "fine answer"}))
    rep = run_cases("t", _cases(3), {"explain": ok})
    path = write_baseline(rep, "unit", tmp_path)
    base = load_baseline(path)
    assert base == {"c0": True, "c1": True, "c2": True}
    assert compare(rep, base).ok
    # a case that passed before and fails now is a regression
    worse = run_cases(
        "t", _cases(3), {"explain": Fixed(SutOutput({"text": "one two three four six seven"}))}
    )
    assert compare(worse, base).regressions == ["c0", "c1", "c2"]
    # a case with no entry fails the check, and a baseline entry for a vanished case is stale
    more = run_cases("t", _cases(4), {"explain": ok})
    assert compare(more, base).missing == ["c3"] and not compare(more, base).ok
    assert compare(run_cases("t", _cases(2), {"explain": ok}), base).stale == ["c2"]
    # a failing case may be in the baseline; it only counts when it later passes
    bad_base = {"c0": False, "c1": True, "c2": True}
    assert compare(rep, bad_base).improvements == ["c0"] and compare(rep, bad_base).ok
    # a run with provider errors can never become a baseline
    flaky = run_cases(
        "t", _cases(2), {"explain": Fixed(SutOutput({"text": ""}, error="llm_unavailable"))}
    )
    with pytest.raises(ValueError):
        write_baseline(flaky, "bad", tmp_path)


def test_cases_have_teeth_a_weakened_guardian_fails_the_safety_cases() -> None:
    cases = [c for c in all_cases() if c.sut == "t0"]
    weak = run_cases("weak", cases, {"t0": T0Sut(Guardian(rules=()))})
    failed = {r.case_id for r in weak.results if not r.passed}
    assert (
        "plan-unverifiable-allergen-blocked" in failed
    )  # nothing blocks the unverifiable allergen


def test_prompt_layout_puts_the_stable_text_first() -> None:
    from vitalmesh_evals.suts import SYSTEM_PROMPT

    user = render_user(FACTS, "why?")
    assert user.startswith("FACTS:") and user.endswith("USER QUESTION: why?")
    assert "2124" not in SYSTEM_PROMPT  # nothing per-request lives in the system prompt


def test_all_cases_have_unique_ids() -> None:
    ids = [c.id for c in all_cases()]
    assert len(ids) == len(set(ids))


# ------------------------------------------------------------------------- resume
class Flaky:
    """Fails with a provider error for chosen case ids, passes the rest; records every run."""

    def __init__(self, failing: set[str]) -> None:
        self.failing = failing
        self.runs: list[str] = []

    def run(self, case: EvalCase) -> SutOutput:
        self.runs.append(case.id)
        if case.id in self.failing:
            return SutOutput({"text": ""}, error="llm_unavailable")
        return SutOutput({"text": "fine answer"})


def _cases(n: int) -> list[EvalCase]:
    return [
        EvalCase(
            id=f"c{i}", category="x", sut="explain", input=INP, graders=(g("max_words", limit=5),)
        )
        for i in range(n)
    ]


def test_a_rerun_resumes_after_provider_errors_without_redoing_finished_cases(
    tmp_path: Path,
) -> None:
    ck = tmp_path / "ck.jsonl"
    cases = _cases(6)
    first = Flaky(failing={"c3", "c4", "c5"})  # the rate limit hits after three good cases
    r1 = run_cases("t", cases, {"explain": first}, checkpoint=ck, config="v1")
    assert r1.passed == 3 and first.runs == ["c0", "c1", "c2", "c3", "c4", "c5"]
    second = Flaky(failing=set())
    r2 = run_cases("t", cases, {"explain": second}, checkpoint=ck, config="v1")
    assert second.runs == ["c3", "c4", "c5"]  # only the unfinished cases ran
    assert r2.passed == 6 and len(r2.results) == 6
    third = Flaky(failing=set())
    run_cases("t", cases, {"explain": third}, checkpoint=ck, config="v1")
    assert third.runs == []  # everything is done: nothing is rerun


def test_a_changed_case_or_config_is_not_resumed(tmp_path: Path) -> None:
    ck = tmp_path / "ck.jsonl"
    run_cases("t", _cases(2), {"explain": Flaky(set())}, checkpoint=ck, config="v1")
    again = Flaky(set())
    run_cases("t", _cases(2), {"explain": again}, checkpoint=ck, config="v2")  # prompt changed
    assert again.runs == ["c0", "c1"]
    edited = _cases(2)
    edited[0] = edited[0].model_copy(update={"graders": (g("max_words", limit=9),)})
    third = Flaky(set())
    run_cases("t", edited, {"explain": third}, checkpoint=ck, config="v2")
    assert third.runs == ["c0"]  # only the edited case reran


def test_a_crashing_sut_is_an_error_and_a_torn_checkpoint_line_is_ignored(tmp_path: Path) -> None:
    class Boom:
        def run(self, case: EvalCase) -> SutOutput:
            raise ConnectionError("redis down")

    ck = tmp_path / "ck.jsonl"
    rep = run_cases("t", _cases(2), {"explain": Boom()}, checkpoint=ck, config="v1")
    assert rep.passed == 0 and rep.results[0].error == "sut_crashed: ConnectionError"
    assert not ck.exists() or ck.read_text() == ""  # errors are never checkpointed
    ck.write_text('{"fp": "abc", "result": {"half-written', encoding="utf-8")
    ok = Flaky(set())
    assert run_cases("t", _cases(2), {"explain": ok}, checkpoint=ck, config="v1").passed == 2


def test_a_torn_checkpoint_tail_does_not_swallow_the_next_entry(tmp_path: Path) -> None:
    ck = tmp_path / "ck.jsonl"
    ck.write_text('{"fp": "abc", "result": {"half-written', encoding="utf-8")  # no newline
    first = Flaky(set())
    run_cases("t", _cases(3), {"explain": first}, checkpoint=ck, config="v1")
    assert first.runs == ["c0", "c1", "c2"]
    second = Flaky(set())
    run_cases("t", _cases(3), {"explain": second}, checkpoint=ck, config="v1")
    assert second.runs == []  # all three survived the torn line before them
    assert ck.read_text().count("\n") >= 4  # the torn line was terminated, not merged


def test_deterministic_cases_are_never_checkpointed_and_the_code_fingerprint_is_stable(
    tmp_path: Path,
) -> None:
    from vitalmesh_evals.runner import code_fingerprint

    ck = tmp_path / "ck.jsonl"
    t0_case = EvalCase(
        id="d", category="x", sut="t0", input=INP, graders=(g("max_words", limit=5),)
    )
    sut = Flaky(set())
    run_cases("t", [t0_case], {"t0": sut}, checkpoint=ck, config="v1")
    run_cases("t", [t0_case], {"t0": sut}, checkpoint=ck, config="v1")
    assert sut.runs == ["d", "d"] and not ck.exists()  # reran: nothing was saved
    assert code_fingerprint() == code_fingerprint() and len(code_fingerprint()) == 16


def test_checkpoint_entries_are_complete_json_lines_on_disk(tmp_path: Path) -> None:
    import json

    ck = tmp_path / "ck.jsonl"
    run_cases("t", _cases(4), {"explain": Flaky(set())}, checkpoint=ck, config="v1")
    lines = ck.read_text().splitlines()
    assert len(lines) == 4
    assert all({"fp", "result"} <= json.loads(line).keys() for line in lines)
