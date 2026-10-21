"""Cohort cache (T1) building blocks: who shares an answer, and how it is personalised (D70).

Pure code, no model. A released answer for a low-risk question can be reused for users with the
same archetype, but only after the numbers that came from THAT user's calculators are swapped for
placeholders and filled in again from the NEW user's calculators. Three safety properties:

* eligibility is narrow: anyone with a declared condition, a medication or a flagged lab never
  shares an answer (their constraints change what is safe), so they always get a full run;
* a template is stored only if every number in the text is either a placeholder for a computed
  claim or a number a cited claim states, and only if re-filling the template from the ORIGINAL
  claims reproduces the original text exactly (a round trip), so a template that cannot be
  personalised faithfully is never cached;
* the cache key covers everything that changes the answer: normalised question, archetype, the
  specialists in the plan and the rules version, so a rules change makes every entry unreachable.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from vitalmesh_wellness.engine import Profile

from vitalmesh_agents.triage import Signals

AGE_BAND_YEARS = 10
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_TOKEN = re.compile(r"\{([a-z0-9_]+)#(\d+)\|(i|c|f\d)\}")
_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")

COMPUTED = "computed"
CITED = "cited"


@dataclass(frozen=True)
class ClaimView:
    """The part of a claim the cache needs; handlers convert from and to the event's claim."""

    text: str
    provenance: str
    source: str = ""
    metric: str = ""
    value: float | None = None
    unit: str = ""


@dataclass(frozen=True)
class Archetype:
    sex: str
    age_band: int
    activity: str
    goal: str
    diet: str
    allergens: tuple[str, ...]


def archetype_of(profile: Profile) -> Archetype:
    return Archetype(
        sex=profile.sex.value,
        age_band=int(profile.age_years // AGE_BAND_YEARS) * AGE_BAND_YEARS,
        activity=profile.activity.value,
        goal=profile.goal.value,
        diet=profile.diet,
        allergens=tuple(sorted(a.strip().lower() for a in profile.allergens)),
    )


def cohort_eligible(signals: Signals) -> bool:
    """Whether this user may share a cached answer at all."""
    return not (signals.has_conditions or signals.abnormal_labs or signals.marked_labs)


def normalise_question(question: str) -> str:
    """Case, spacing and punctuation do not make a different question; wording does."""
    return _SPACE.sub(" ", _PUNCT.sub(" ", question.casefold())).strip()


def cache_key(
    question: str, archetype: Archetype, specialists: Sequence[str], rules_version: str
) -> str:
    material = {
        "q": normalise_question(question),
        "a": [
            archetype.sex,
            archetype.age_band,
            archetype.activity,
            archetype.goal,
            archetype.diet,
            list(archetype.allergens),
        ],
        "s": sorted(specialists),
        "v": rules_version,
    }
    digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    return f"cohort:{digest}"


# --------------------------------------------------------------------------- templates
@dataclass(frozen=True)
class TemplateClaim:
    provenance: str
    metric: str = ""
    ordinal: int = 0  # the n-th computed claim with this metric, in order
    text: str = ""  # cited claims keep their sentence; computed ones are rebuilt from fresh data
    source: str = ""


@dataclass(frozen=True)
class Template:
    text: str
    claims: tuple[TemplateClaim, ...]


def _number(raw: str) -> float:
    return float(raw.replace(",", ""))


def _style(raw: str) -> str:
    if "." in raw:
        return f"f{len(raw.split('.')[1])}"
    return "c" if "," in raw else "i"


def _render(value: float, style: str) -> str:
    if style == "c":
        return f"{round(value):,}"
    if style == "i":
        return str(round(value))
    return f"{value:.{int(style[1])}f}"


def _ordinals(claims: Sequence[ClaimView]) -> list[tuple[str, int, ClaimView]]:
    seen: dict[str, int] = {}
    out: list[tuple[str, int, ClaimView]] = []
    for c in claims:
        if c.provenance != COMPUTED or c.value is None or not c.metric:
            continue
        n = seen.get(c.metric, 0)
        seen[c.metric] = n + 1
        out.append((c.metric, n, c))
    return out


def build_template(text: str, claims: Sequence[ClaimView]) -> Template | None:
    """None when the answer cannot be cached faithfully."""
    if any(c.provenance not in (COMPUTED, CITED) for c in claims):
        return None  # inferred or unknown statements are about this user's reasoning: not shared
    computed = _ordinals(claims)
    if any(c.provenance == COMPUTED for c in claims) and len(computed) != sum(
        c.provenance == COMPUTED for c in claims
    ):
        return None  # a computed claim without a metric and value cannot be re-filled
    cited_numbers = {
        _number(m) for c in claims if c.provenance == CITED for m in _NUMBER.findall(c.text)
    }

    def swap(match: re.Match[str]) -> str:
        raw = match.group(0)
        value = _number(raw)
        hits = [
            (metric, n)
            for metric, n, claim in computed
            if claim.value is not None and round(claim.value, 1) == round(value, 1)
        ]
        if len(hits) == 1:
            return f"{{{hits[0][0]}#{hits[0][1]}|{_style(raw)}}}"
        if not hits and value in cited_numbers:
            return raw
        # no source for the number, or two calculators gave this value for THIS user and another
        # user's values may differ: which placeholder it is cannot be told, so do not cache
        raise _Uncacheable(raw)

    try:
        body = _NUMBER.sub(swap, text)
    except _Uncacheable:
        return None
    template = Template(
        text=body,
        claims=tuple(
            TemplateClaim(CITED, text=c.text, source=c.source)
            if c.provenance == CITED
            else TemplateClaim(COMPUTED, metric=c.metric, ordinal=_ordinal_of(c, computed))
            for c in claims
        ),
    )
    # The round trip: filling the template with the original data must give the original text.
    again = personalise(template, claims)
    if again is None or again[0] != text:
        return None
    return template


def _ordinal_of(claim: ClaimView, computed: list[tuple[str, int, ClaimView]]) -> int:
    for _, n, c in computed:
        if c is claim:
            return n
    return 0


class _Uncacheable(Exception):
    pass


def personalise(
    template: Template, fresh: Sequence[ClaimView]
) -> tuple[str, list[ClaimView]] | None:
    """Fill the template from the new user's own computed claims. None when any placeholder has
    no matching fresh claim (then the run is a miss and goes to a full run)."""
    by_slot = {(metric, n): claim for metric, n, claim in _ordinals(fresh)}
    claims: list[ClaimView] = []
    for t in template.claims:
        if t.provenance == CITED:
            claims.append(ClaimView(text=t.text, provenance=CITED, source=t.source))
            continue
        got = by_slot.get((t.metric, t.ordinal))
        if got is None:
            return None
        claims.append(got)

    missing = False

    def fill(match: re.Match[str]) -> str:
        nonlocal missing
        got = by_slot.get((match.group(1), int(match.group(2))))
        if got is None or got.value is None:
            missing = True
            return ""
        return _render(got.value, match.group(3))

    text = _TOKEN.sub(fill, template.text)
    if missing:
        return None
    return text, claims
