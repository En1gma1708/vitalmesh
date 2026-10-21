"""Deterministic guard around the LLM explainer (D61).

The system prompt asks the model to decline harmful or override requests, but a prompt is a
request, not a guarantee: the first prompt-only attempt fixed some cases and broke others. This
module enforces the same rules in code, in two places:

* ``screen_question`` (before the model): a blocklist of override, extraction, output-hijack and
  unsafe-advice phrasings. A match gets a fixed decline plus the real facts and never reaches the
  model (so it also costs no quota). A blocklist is NOT complete: a rephrasing can slip past it,
  and then the model and the checks below are the defence.
* ``check_answer`` (after the model): the answer may only contain numbers that are in the facts,
  must not contain the system prompt, and must not be empty. A failing answer is replaced by the
  no-model template, so the user never sees it.

Both are pure functions with no I/O. The patterns were written after reading the eval failures,
so scores on the injection suite overstate how well they generalise (disclosed in D61).
"""

from __future__ import annotations

from typing import Any

from vitalmesh_concierge.screen import REFUSALS, screen_question

from vitalmesh_evals.graders import _close, numbers_in
from vitalmesh_evals.suts import (
    LEAK_SNIPPETS,
    Explainer,
    SutOutput,
    TemplateExplainer,
)


def decline_text(facts: list[dict[str, Any]], category: str = "unsafe_advice") -> str:
    stated = " ".join(f["text"] for f in facts)
    return f"{REFUSALS[category]} From your recommendation: {stated}"


def _allowed_numbers(facts: list[dict[str, Any]]) -> list[float]:
    allowed = [float(f["value"]) for f in facts]
    for f in facts:
        allowed.extend(numbers_in(f["text"]))
    return allowed


def check_answer(text: str, facts: list[dict[str, Any]]) -> str | None:
    """Return why a model answer must not be shown, or None."""
    if not text.strip():
        return "empty"
    low = text.lower()
    if any(s.lower() in low for s in LEAK_SNIPPETS):
        return "prompt_leak"
    allowed = _allowed_numbers(facts)
    if any(not any(_close(v, a) for a in allowed) for v in numbers_in(text)):
        return "ungrounded_number"
    return None


class GuardedExplainer:
    """Screens the question, then checks the answer; a refusal or a rejected answer is replaced by
    deterministic text. Tokens already spent are still reported."""

    def __init__(self, inner: Explainer, fallback: Explainer | None = None) -> None:
        self._inner = inner
        self._fallback = fallback or TemplateExplainer()

    def explain(self, facts: list[dict[str, Any]], question: str) -> SutOutput:
        category = screen_question(question)
        if category is not None:
            return SutOutput(
                {"text": decline_text(facts, category), "guard": f"declined:{category}"},
                provider="guard",
            )
        out = self._inner.explain(facts, question)
        if out.error is not None:
            return out
        reason = check_answer(out.output["text"], facts)
        if reason is None:
            return out
        fb = self._fallback.explain(facts, question)
        fb.output = {**fb.output, "guard": f"rejected:{reason}"}
        fb.provider = f"guard-fallback({out.provider})"
        fb.prompt_tokens = out.prompt_tokens
        fb.completion_tokens = out.completion_tokens
        fb.cached = out.cached
        return fb
