"""Guardian gate for T3 drafts (D68): pure code, no model, fails closed.

The Synthesizer's text is checked here AGAIN, independently: the gate trusts nothing the
Synthesizer asserted (its own number check, its fallback flag). It runs

1. the shared wellness Guardian (rules version ``RULES_VERSION``) on the draft's claims and the
   user's declared constraints, so an unsafe zone, a wrong unit or an unknown provenance blocks a
   T3 answer exactly as it blocks a T0 one; and
2. the T3 rules below on the prose, the citations and the disclosure.

A rule that raises is a violation. A missing profile is a violation (the constraints cannot be
applied, so nothing may be released). The blocklist rule is the first layer only; it is wording
based and will miss phrasings (the measured lesson of D61), so it never replaces the Checker.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from vitalmesh.events.v1 import common_pb2
from vitalmesh_specialists.agent import close, numbers
from vitalmesh_wellness.claims import Claim, Provenance, Quantity, Recommendation, UserConstraints
from vitalmesh_wellness.guardian import RULES_VERSION, Guardian, Violation

from vitalmesh_agents.advice import claim_numbers

T3_RULES_VERSION = f"{RULES_VERSION}+t3-1"
MAX_WORDS = 200

_PROVENANCE = {
    common_pb2.PROVENANCE_COMPUTED: Provenance.COMPUTED,
    common_pb2.PROVENANCE_CITED: Provenance.CITED,
    common_pb2.PROVENANCE_INFERRED: Provenance.INFERRED,
    common_pb2.PROVENANCE_UNKNOWN: Provenance.UNKNOWN,
}

# Wording a wellness assistant must not produce. Conservative and incomplete on purpose.
_ACTION_ON_MEDICATION = re.compile(
    r"\b(?:stop|start|begin|increase|decrease|reduce|raise|lower|change|skip|double|halve|"
    r"adjust|take)\b[^.!?]{0,40}\b(?:medication|medicine|medicines|dose|doses|dosage|pill|pills|"
    r"prescription|insulin|statin|statins|tablet|tablets)\b",
    re.IGNORECASE,
)
_DIAGNOSIS = re.compile(
    r"\byou (?:have|likely have|probably have|may have|might have|are suffering from|suffer from)"
    r" (?:type [12] )?(?:diabetes|hypertension|heart disease|cancer|anaemia|anemia|"
    r"hypothyroidism|hyperthyroidism|kidney disease|liver disease|a heart condition)\b"
    r"|\b(?:i|we) (?:diagnose|diagnosed)\b|\bdiagnosis (?:is|of)\b",
    re.IGNORECASE,
)


def draft_to_recommendation(draft: Any, user: UserConstraints) -> Recommendation:
    """The wire draft as the domain object the shared Guardian understands. Raises ``ValueError``
    for anything the domain model refuses (an unspecified provenance, a computed claim without a
    quantity): the caller treats that as a violation, never as a crash."""
    claims: list[Claim] = []
    for c in draft.claims:
        prov = _PROVENANCE.get(c.provenance)
        if prov is None:
            raise ValueError("a claim has no provenance")
        quantity = None
        if c.HasField("quantity"):
            if not c.metric:
                raise ValueError("a claim has a quantity but no metric")
            quantity = Quantity(metric=c.metric, value=c.quantity.value, unit=c.quantity.unit)
        claims.append(
            Claim(
                text=c.text,
                provenance=prov,
                source=c.source if c.HasField("source") else None,
                quantity=quantity,
            )
        )
    return Recommendation(
        recommendation_id=draft.recommendation_id or "unknown",
        user=user,
        claims=tuple(claims),
    )


def _v(rule: str, message: str, claim_text: str | None = None) -> Violation:
    return Violation(rule=rule, message=message, claim_text=claim_text)


def rule_text_present_and_bounded(draft: Any) -> list[Violation]:
    words = len(re.findall(r"\b\w+\b", draft.text))
    if words == 0:
        return [_v("text_present", "the reply is empty")]
    if words > MAX_WORDS:
        return [_v("text_bounded", f"the reply has {words} words, the limit is {MAX_WORDS}")]
    return []


def rule_numbers_grounded(draft: Any) -> list[Violation]:
    allowed = claim_numbers(list(draft.claims))
    stray = sorted(v for v in numbers(draft.text) if not any(close(v, a) for a in allowed))
    if stray:
        return [_v("numbers_grounded", f"numbers {stray} are not in any claim")]
    return []


def rule_cited_claims_have_citations(draft: Any) -> list[Violation]:
    have = {f"{c.document} {c.locator}" for c in draft.citations}
    return [
        _v("cited_has_citation", "a cited claim has no matching verified citation", c.text)
        for c in draft.claims
        if c.provenance == common_pb2.PROVENANCE_CITED and c.source not in have
    ]


def rule_no_medication_or_diagnosis_wording(draft: Any) -> list[Violation]:
    out: list[Violation] = []
    if _ACTION_ON_MEDICATION.search(draft.text):
        out.append(_v("no_medication_advice", "the reply tells the user to act on medication"))
    if _DIAGNOSIS.search(draft.text):
        out.append(_v("no_diagnosis", "the reply states or implies a diagnosis"))
    return out


def rule_partial_is_disclosed(draft: Any) -> list[Violation]:
    if draft.partial and "partial" not in draft.text.lower():
        return [_v("partial_disclosed", "a partial answer must say it is partial")]
    return []


def rule_risk_is_set(draft: Any) -> list[Violation]:
    if draft.risk not in (common_pb2.RISK_LOW, common_pb2.RISK_MEDIUM, common_pb2.RISK_HIGH):
        return [_v("risk_set", "the draft has no valid risk level")]
    return []


T3Rule = Callable[[Any], list[Violation]]
T3_RULES: tuple[T3Rule, ...] = (
    rule_text_present_and_bounded,
    rule_numbers_grounded,
    rule_cited_claims_have_citations,
    rule_no_medication_or_diagnosis_wording,
    rule_partial_is_disclosed,
    rule_risk_is_set,
)


@dataclass(frozen=True)
class GateVerdict:
    cleared: bool
    violations: tuple[Violation, ...]
    rules_version: str = T3_RULES_VERSION


def evaluate_draft(
    draft: Any,
    user: UserConstraints | None,
    guardian: Guardian | None = None,
    extra_rules: Sequence[T3Rule] = (),
) -> GateVerdict:
    violations: list[Violation] = []
    if user is None:
        violations.append(
            _v("profile_known", "no profile: the user's constraints cannot be applied")
        )
    else:
        try:
            rec = draft_to_recommendation(draft, user)
            violations.extend((guardian or Guardian()).evaluate(rec).violations)
        except ValueError as exc:  # includes pydantic's ValidationError
            violations.append(_v("claims_valid", f"the claims are not valid: {str(exc)[:200]}"))
    for rule in (*T3_RULES, *extra_rules):
        try:
            violations.extend(rule(draft))
        except Exception as exc:  # fail closed: a rule that cannot run blocks
            name = getattr(rule, "__name__", "unknown_rule")
            violations.append(_v(name, f"rule could not run ({type(exc).__name__}); blocked"))
    return GateVerdict(cleared=not violations, violations=tuple(violations))
