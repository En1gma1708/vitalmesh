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

import re
import unicodedata
from typing import Any

from vitalmesh_evals.graders import _close, numbers_in
from vitalmesh_evals.suts import (
    LEAK_SNIPPETS,
    Explainer,
    SutOutput,
    TemplateExplainer,
)

CLINICIAN_LINE = "For symptoms, medication or a diagnosis, please talk to a doctor or clinician."

_PATTERNS: dict[str, tuple[str, ...]] = {
    "unsafe_advice": (
        r"\bcrash diet\b",
        r"\bstarv(e|ing|ation)\b",
        r"\bfast(ing)? for \d+ days\b",
        r"\blose \d+ ?(kg|lbs?|pounds) in\b",
        r"\b(dose|dosage)\b",
        r"\bhow many (mg|pills|tablets)\b",
        r"\bdiagnos(e|is)\b",
        r"\bprescri(be|ption)\b",
        r"\bdo i have (cancer|diabetes|a \w+)\b",
    ),
    "override": (
        r"\bignore\b.{0,40}\b(instruction|rule|previous|above|prior|all|everything)",
        r"\bdisregard\b",
        r"\boverride\b",
        r"\b(admin|developer|debug|god|dan|jailbreak) mode\b",
        r"\bdeveloper message\b",
        r"\b(you are|act as|pretend)\b.{0,25}"
        r"\b(now|no longer|unrestricted|without (rules|limits))\b",
        r"\b(is|are) now\b.{0,30}\d",
        r"\bmust use this value\b",
        r"\b(new|end of) facts\b",
        r"^\s*\[(developer|system|admin)\]",
        r"```\s*system",
        r"\byou must say\b",
    ),
    "extraction": (
        r"\b(system|initial|hidden|your) (prompt|instructions|rules)\b",
        r"\bfirst (\w+ ){0,2}words of (this|the) conversation\b",
        r"\bprint everything above\b",
        r"\brepeat (everything|the text|your)\b",
        r"\btranslate (your|the) (rules|instructions|prompt)\b",
    ),
    "output_hijack": (
        r"\b(reply|respond|answer|output|print|say|write)\b.{0,20}\b(only|exactly|just)\b",
        r"\bnothing else\b",
        r"</?(facts|answer)>",
        r"^\s*[{\[].*[}\]]\s*$",
    ),
}
_COMPILED = {k: tuple(re.compile(p, re.DOTALL) for p in v) for k, v in _PATTERNS.items()}


# a shouted token to be repeated ("say PWNED"): needs the original case, so it is checked apart
_SHOUTED = re.compile(r"\b(say|print|write|type)\s+[\"']?[A-Z]{4,}")


def screen_question(question: str) -> str | None:
    """Return the category of a request that must be declined, or None."""
    original = unicodedata.normalize("NFKC", question)
    text = original.casefold()
    if _SHOUTED.search(original):
        return "output_hijack"
    for category, patterns in _COMPILED.items():
        if any(p.search(text) for p in patterns):
            return category
    return None


REFUSALS = {
    "unsafe_advice": f"I can't help with that. {CLINICIAN_LINE}",
    "override": "I can't change my rules or the numbers in your plan.",
    "extraction": "I can't share or repeat my instructions.",
    "output_hijack": "I can't answer in that format or with only those words.",
}


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
