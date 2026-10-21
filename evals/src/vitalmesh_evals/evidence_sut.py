"""System under test for the Evidence agent (D64).

The agent already verifies its own output; this SUT re-checks the result independently, straight
against the database (does each cited quote really occur in the stored chunk with that document
and page?), so a bug in the agent's verifier cannot silently pass its own work.
"""

from __future__ import annotations

import re
import time
from typing import Any

import psycopg
from vitalmesh_evidence import EvidenceAgent, EvidenceUnavailable

from vitalmesh_evals.case import EvalCase
from vitalmesh_evals.suts import SutOutput

_SPACE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _SPACE.sub(" ", text).strip().lower()


class EvidenceSut:
    def __init__(self, agent: EvidenceAgent, dsn: str) -> None:
        self._agent = agent
        self._conn = psycopg.connect(dsn, autocommit=True)

    def _quote_in_source(self, doc_id: str, locator: str | None, quote: str) -> bool:
        rows = self._conn.execute(
            "SELECT text FROM chunks WHERE doc_id = %s AND locator IS NOT DISTINCT FROM %s",
            (doc_id, locator),
        ).fetchall()
        wanted = _norm(quote)
        return any(wanted in _norm(r[0]) for r in rows)

    def run(self, case: EvalCase) -> SutOutput:
        started = time.perf_counter()
        try:
            ans = self._agent.answer(case.input["question"])
        except EvidenceUnavailable as exc:
            return SutOutput({"status": "unavailable", "text": ""}, error=f"llm_unavailable: {exc}")
        sentences: list[dict[str, Any]] = [
            {
                "text": s.text,
                "doc_id": s.citation.doc_id,
                "locator": s.citation.locator,
                "licence": s.citation.licence,
                "quote": s.citation.quote,
                "quote_in_source": self._quote_in_source(
                    s.citation.doc_id, s.citation.locator, s.citation.quote
                ),
            }
            for s in ans.sentences
        ]
        return SutOutput(
            {
                "status": ans.status,
                "reason": ans.reason,
                "attempts": ans.attempts,
                "text": " ".join(s["text"] for s in sentences),
                "sentences": sentences,
            },
            provider="evidence-agent",
            prompt_tokens=ans.prompt_tokens,
            completion_tokens=ans.completion_tokens,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
