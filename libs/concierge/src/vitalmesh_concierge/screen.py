"""Input screen (D61, moved here in D67): a blocklist of override, extraction, output-hijack and
unsafe-advice phrasings. A match gets a fixed refusal and never reaches a model.

A blocklist is NOT complete: it caught 0 of 8 held-out phrasings in the D61 evals, so it is the
first defence only; the models' own refusals and the output checks are the others. Pure functions.
"""

from __future__ import annotations

import re
import unicodedata

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
