"""Safety Guardian: deterministic rules over a recommendation. Pure code, no I/O, fail closed.

The same library runs on consume, on publish, and as the authoritative final gate
(docs/EVENT_BACKBONE.md section 10). A rule that raises is itself a violation: a check that
cannot run blocks the recommendation, it never passes it through.

Thresholds below are configured policy for a general-wellness product, stamped with
``RULES_VERSION`` so events checked under older rules can be detected. They are not medical
advice, and the knowledge-graph contraindication rules (Phase 1, later slice) extend them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from vitalmesh_wellness.claims import Claim, Provenance, Quantity, Recommendation

RULES_VERSION = "1"

# Metric names the rules understand, with the only unit each may use.
METRIC_ENERGY = "daily_energy_target"
METRIC_HR_ZONE = "target_hr_zone"
METRIC_SLEEP = "sleep_target"
EXPECTED_UNIT = {METRIC_ENERGY: "kcal", METRIC_HR_ZONE: "zone", METRIC_SLEEP: "h"}

ENERGY_FLOOR_KCAL = {"female": 1200.0, "male": 1500.0}
ENERGY_CEILING_KCAL = 6000.0
SLEEP_TARGET_HOURS = (7.0, 10.0)
# Conditions that cap exercise intensity at zone 3 until a clinician clears more.
INTENSITY_LIMITING_CONDITIONS = frozenset({"cardiac", "uncontrolled_hypertension"})
MAX_ZONE_WITH_LIMITING_CONDITION = 3.0


class Severity(StrEnum):
    BLOCK = "block"


class Violation(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule: str
    message: str
    severity: Severity = Severity.BLOCK
    claim_text: str | None = None


class Verdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    cleared: bool
    violations: tuple[Violation, ...]
    rules_version: str = RULES_VERSION


Rule = Callable[[Recommendation], list[Violation]]


def _quantities(rec: Recommendation, metric: str) -> list[tuple[Claim, Quantity]]:
    return [(c, c.quantity) for c in rec.claims if c.quantity and c.quantity.metric == metric]


def rule_has_claims(rec: Recommendation) -> list[Violation]:
    if rec.claims:
        return []
    return [Violation(rule="has_claims", message="recommendation has no claims to verify")]


def rule_no_unknown_provenance(rec: Recommendation) -> list[Violation]:
    return [
        Violation(
            rule="no_unknown_provenance",
            message="a claim with unknown provenance is never treated as safe",
            claim_text=c.text,
        )
        for c in rec.claims
        if c.provenance is Provenance.UNKNOWN
    ]


def rule_units_match(rec: Recommendation) -> list[Violation]:
    out: list[Violation] = []
    for c in rec.claims:
        q = c.quantity
        if q and q.metric in EXPECTED_UNIT and q.unit != EXPECTED_UNIT[q.metric]:
            out.append(
                Violation(
                    rule="units_match",
                    message=f"{q.metric} must use unit {EXPECTED_UNIT[q.metric]!r}, got {q.unit!r}",
                    claim_text=c.text,
                )
            )
    return out


def rule_energy_bounds(rec: Recommendation) -> list[Violation]:
    sex = rec.user.sex.lower()
    if sex not in ENERGY_FLOOR_KCAL:
        # The floor depends on sex; without a known value we cannot apply it, so block.
        if _quantities(rec, METRIC_ENERGY):
            return [Violation(rule="energy_bounds", message=f"no energy floor for sex {sex!r}")]
        return []
    floor = ENERGY_FLOOR_KCAL[sex]
    out: list[Violation] = []
    for c, q in _quantities(rec, METRIC_ENERGY):
        if not floor <= q.value <= ENERGY_CEILING_KCAL:
            out.append(
                Violation(
                    rule="energy_bounds",
                    message=f"daily energy {q.value:.0f} kcal is outside [{floor:.0f}, "
                    f"{ENERGY_CEILING_KCAL:.0f}] for this user",
                    claim_text=c.text,
                )
            )
    return out


def rule_intensity_cap(rec: Recommendation) -> list[Violation]:
    if not rec.user.conditions & INTENSITY_LIMITING_CONDITIONS:
        return []
    return [
        Violation(
            rule="intensity_cap",
            message=f"zone {q.value:g} exceeds zone {MAX_ZONE_WITH_LIMITING_CONDITION:g} "
            "for a user with an intensity-limiting condition",
            claim_text=c.text,
        )
        for c, q in _quantities(rec, METRIC_HR_ZONE)
        if q.value > MAX_ZONE_WITH_LIMITING_CONDITION
    ]


def rule_sleep_bounds(rec: Recommendation) -> list[Violation]:
    low, high = SLEEP_TARGET_HOURS
    return [
        Violation(
            rule="sleep_bounds",
            message=f"sleep target {q.value:g} h is outside [{low:g}, {high:g}] h",
            claim_text=c.text,
        )
        for c, q in _quantities(rec, METRIC_SLEEP)
        if not low <= q.value <= high
    ]


DEFAULT_RULES: tuple[Rule, ...] = (
    rule_has_claims,
    rule_no_unknown_provenance,
    rule_units_match,
    rule_energy_bounds,
    rule_intensity_cap,
    rule_sleep_bounds,
)


@dataclass(frozen=True)
class Guardian:
    rules: tuple[Rule, ...] = DEFAULT_RULES

    def evaluate(self, rec: Recommendation) -> Verdict:
        violations: list[Violation] = []
        for rule in self.rules:
            try:
                violations.extend(rule(rec))
            except Exception as exc:  # fail closed: a rule that cannot run blocks
                violations.append(
                    Violation(
                        rule=getattr(rule, "__name__", "unknown_rule"),
                        message=f"rule could not run ({type(exc).__name__}); blocked",
                    )
                )
        return Verdict(cleared=not violations, violations=tuple(violations))
