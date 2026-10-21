"""Deterministic verification of a model's draft against the passages it was given.

The model is never trusted to cite correctly. A sentence passes only if (1) its ``cite`` names a
passage that was retrieved, (2) its ``quote`` really occurs in that passage (whitespace and case
ignored) and is long enough to be evidence, and (3) every number in the sentence also occurs in
the quote. Numbers written as words ("two") are not checked: a known gap, listed in D64.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from vitalmesh_evidence.types import (
    AnswerSentence,
    Citation,
    Draft,
    Passage,
)

MIN_QUOTE_CHARS = 25
MAX_SENTENCES = 4
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_SPACE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _SPACE.sub(" ", text).strip().lower()


def _numbers(text: str) -> set[float]:
    out: set[float] = set()
    for m in _NUMBER.findall(text):
        try:
            out.add(float(m.replace(",", "")))
        except ValueError:
            continue
    return out


def verify(draft: Draft, passages: Sequence[Passage]) -> list[str]:
    """Return a list of problems; empty means every sentence is grounded."""
    if draft.insufficient:
        return []
    if not draft.sentences:
        return ["the draft has no sentences and does not say the evidence is insufficient"]
    if len(draft.sentences) > MAX_SENTENCES:
        return [f"more than {MAX_SENTENCES} sentences"]
    by_ref = {p.ref: p for p in passages}
    problems: list[str] = []
    for i, s in enumerate(draft.sentences, start=1):
        passage = by_ref.get(s.cite)
        if passage is None:
            problems.append(f"sentence {i}: cite '{s.cite}' is not one of the passages")
            continue
        if len(s.quote.strip()) < MIN_QUOTE_CHARS:
            problems.append(f"sentence {i}: the quote is too short to be evidence")
            continue
        if _norm(s.quote) not in _norm(passage.text):
            problems.append(f"sentence {i}: the quote does not appear in passage {s.cite}")
            continue
        stray = sorted(_numbers(s.text) - _numbers(s.quote))
        if stray:
            problems.append(f"sentence {i}: numbers {stray} are not in the quote")
    return problems


def to_answer_sentences(draft: Draft, passages: Sequence[Passage]) -> tuple[AnswerSentence, ...]:
    """Attach the full citation to each verified sentence (call only after ``verify`` is clean)."""
    by_ref = {p.ref: p for p in passages}
    out: list[AnswerSentence] = []
    for s in draft.sentences:
        p = by_ref[s.cite]
        out.append(
            AnswerSentence(
                text=s.text,
                citation=Citation(
                    doc_id=p.doc_id,
                    locator=p.locator,
                    title=p.title,
                    source_url=p.source_url,
                    licence=p.licence,
                    quote=s.quote.strip(),
                ),
            )
        )
    return tuple(out)
