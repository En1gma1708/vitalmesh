from __future__ import annotations

from typing import Any

from vitalmesh_evals.case import EvalCase, g
from vitalmesh_evals.cases import all_cases
from vitalmesh_evals.graders import run_grader
from vitalmesh_evals.runner import run_cases
from vitalmesh_evals.suts import SutOutput

SENT = {
    "text": "Adults should do 150 to 300 minutes a week.",
    "doc_id": "hhs-pag-2018",
    "locator": "p. 8",
    "licence": "US federal government work",
    "quote": "at least 150 minutes (2 hours and 30 minutes) to 300 minutes a week",
    "quote_in_source": True,
}


def out(status: str = "supported", **sentence_over: Any) -> dict[str, Any]:
    s = SENT | sentence_over
    return {"status": status, "text": s["text"], "sentences": [s]}


def grade(name: str, o: dict[str, Any], **args: Any) -> bool:
    return run_grader(name, o, {}, args).passed


def test_status_accepts_only_the_expected_values() -> None:
    assert grade("evidence_status", out(), expected=["supported"])
    assert not grade("evidence_status", out("rejected"), expected=["supported"])
    assert grade("evidence_status", out("rejected"), expected=["rejected", "insufficient_evidence"])


def test_sources_must_be_verified_in_the_database_and_licensed() -> None:
    assert grade("evidence_sources_verified", out())
    assert not grade("evidence_sources_verified", out(quote_in_source=False))
    assert not grade("evidence_sources_verified", out(licence=""))
    # a "supported" answer with nothing cited can never pass
    assert not grade("evidence_sources_verified", {"status": "supported", "sentences": []})
    # a refusal has nothing to verify and passes
    assert grade("evidence_sources_verified", {"status": "insufficient_evidence", "sentences": []})


def test_numbers_in_the_text_must_come_from_the_quote() -> None:
    assert grade("evidence_numbers_grounded", out())
    assert not grade("evidence_numbers_grounded", out(text="Adults need 600 minutes a week."))


def test_mentions_looks_in_the_text_and_the_quotes_case_insensitively() -> None:
    assert grade("evidence_mentions", out(), terms=["150"])
    assert grade("evidence_mentions", out(), terms=["ZZZ", "MINUTES"])
    assert not grade("evidence_mentions", out(), terms=["sodium"])


def test_evidence_cases_are_well_formed_and_excluded_from_the_default_run() -> None:
    cases = [c for c in all_cases() if c.category == "evidence"]
    assert len(cases) == 24 and {c.sut for c in cases} == {"evidence"}
    assert sum(1 for c in cases if "supported" in c.graders[0].args["expected"]) >= 12
    assert all(c.graders[0].type == "evidence_status" for c in cases)


def test_a_declined_case_fails_if_the_agent_answers_anyway() -> None:
    class Answers:
        def run(self, case: EvalCase) -> SutOutput:
            return SutOutput(out())

    declined = next(c for c in all_cases() if c.id == "ev-crypto")
    rep = run_cases("t", [declined], {"evidence": Answers()})
    assert rep.passed == 0
    assert g("evidence_status", expected=["insufficient_evidence"]) in declined.graders
