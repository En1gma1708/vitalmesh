"""Teeth test for the evidence evals: even if the agent's own verifier were switched off, the
SUT's independent database check must catch a fabricated quote (needs the live Postgres)."""

from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest
from vitalmesh_evals.cases import all_cases
from vitalmesh_evals.evidence_sut import EvidenceSut
from vitalmesh_evals.graders import run_grader
from vitalmesh_evidence import EvidenceAgent, Passage
from vitalmesh_evidence import agent as agent_module
from vitalmesh_llm import LLMRequest, LLMResponse

pytestmark = pytest.mark.integration

DSN = "postgresql://vitalmesh@127.0.0.1:5432/vitalmesh"


class OneShotRouter:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def complete(self, req: LLMRequest, max_wait_s: float = 0.0) -> LLMResponse:
        return LLMResponse(
            text=json.dumps(self.payload),
            provider="fake",
            model="fake",
            prompt_tokens=1,
            completion_tokens=1,
            latency_ms=0.0,
            finish_reason="stop",
        )


def passage_from_db() -> Passage:
    try:
        with psycopg.connect(DSN) as c:
            row = c.execute(
                "SELECT c.doc_id, c.locator, c.text, d.title, d.source_url, d.licence "
                "FROM chunks c JOIN documents d USING (doc_id) "
                "WHERE c.text ILIKE '%2,300 milligrams%' LIMIT 1"
            ).fetchone()
    except psycopg.errors.UndefinedTable:
        row = None  # a fresh database (CI): no corpus tables yet
    if row is None:
        pytest.skip("the guideline corpus is not loaded: uv run python scripts/build_corpus.py")
    return Passage(
        ref="c1",
        doc_id=row[0],
        locator=row[1],
        text=row[2],
        title=row[3],
        source_url=row[4],
        licence=row[5],
    )


class Fixed:
    def __init__(self, p: Passage) -> None:
        self.p = p

    def retrieve(self, question: str) -> tuple[list[Passage], float]:
        return [self.p], 0.9


def run(
    p: Passage, quote: str, monkeypatch: pytest.MonkeyPatch, verifier_off: bool
) -> dict[str, Any]:
    if verifier_off:
        monkeypatch.setattr(agent_module, "verify", lambda draft, passages: [])
    payload = {
        "insufficient": False,
        "sentences": [{"text": "Sodium should be under 2,300 mg.", "cite": "c1", "quote": quote}],
    }
    agent = EvidenceAgent(OneShotRouter(payload), Fixed(p))
    case = next(c for c in all_cases() if c.id == "ev-sodium")
    return EvidenceSut(agent, DSN).run(case).output


def test_a_real_quote_passes_the_independent_source_check(monkeypatch: pytest.MonkeyPatch) -> None:
    p = passage_from_db()
    start = p.text.index("2,300 milligrams")
    out = run(p, p.text[max(0, start - 20) : start + 40], monkeypatch, verifier_off=False)
    assert out["status"] == "supported"
    assert run_grader("evidence_sources_verified", out, {}, {}).passed


def test_a_fabricated_quote_is_caught_even_with_the_agents_verifier_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = passage_from_db()
    fake = "Everyone should limit sodium to under 2,300 mg, according to the guidelines."
    out = run(p, fake, monkeypatch, verifier_off=True)
    assert out["status"] == "supported"  # the switched-off verifier let it through ...
    assert not run_grader("evidence_sources_verified", out, {}, {}).passed  # ... the eval did not
