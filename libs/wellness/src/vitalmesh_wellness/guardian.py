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
from vitalmesh_wellness.recipes import ALLERGENS, BY_ID, DIET_NAMES, PORTION_MAX, PORTION_MIN

RULES_VERSION = "2"

# Metric names the rules understand, with the only unit each may use.
METRIC_ENERGY = "daily_energy_target"
METRIC_HR_ZONE = "target_hr_zone"
METRIC_SLEEP = "sleep_target"
METRIC_PLAN_ENERGY = "meal_plan_avg_day_energy"
EXPECTED_UNIT = {
    METRIC_ENERGY: "kcal",
    METRIC_HR_ZONE: "zone",
    METRIC_SLEEP: "h",
    METRIC_PLAN_ENERGY: "kcal",
}
PLAN_TARGET_TOLERANCE = 0.15  # each planned day must land within 15% of the energy target
PLAN_TOTAL_EPSILON_KCAL = 0.5

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


def rule_meal_plan_safe(rec: Recommendation) -> list[Violation]:
    """Every entry is re-checked against the catalogue, never against what the planner says."""
    out: list[Violation] = []
    user = rec.user
    diet = DIET_NAMES.get(user.diet)
    if rec.meal_plan and diet is None:
        out.append(Violation(rule="meal_plan_safe", message=f"unknown diet {user.diet!r}"))
    for a in sorted(user.allergens - ALLERGENS) if rec.meal_plan else ():
        out.append(
            Violation(rule="meal_plan_safe", message=f"cannot verify allergen {a!r}; blocked")
        )
    for e in rec.meal_plan:
        recipe = BY_ID.get(e.recipe_id)
        label = f"{e.day} {e.meal} {e.recipe_id}"
        if recipe is None:
            out.append(Violation(rule="meal_plan_safe", message=f"unknown recipe ({label})"))
            continue
        if e.meal != recipe.meal.value:
            out.append(Violation(rule="meal_plan_safe", message=f"wrong meal slot ({label})"))
        if diet is not None and recipe.diet > diet:
            out.append(Violation(rule="meal_plan_safe", message=f"does not fit the diet ({label})"))
        hit = recipe.allergens & user.allergens
        if hit:
            out.append(
                Violation(
                    rule="meal_plan_safe",
                    message=f"contains an allergen the user avoids: {sorted(hit)} ({label})",
                )
            )
        if not PORTION_MIN <= e.scale <= PORTION_MAX:
            out.append(Violation(rule="meal_plan_safe", message=f"portion out of range ({label})"))
    return out


def rule_meal_plan_energy(rec: Recommendation) -> list[Violation]:
    if not rec.meal_plan:
        return []
    out: list[Violation] = []
    targets = _quantities(rec, METRIC_ENERGY)
    claimed = _quantities(rec, METRIC_PLAN_ENERGY)
    if len(targets) != 1 or len(claimed) != 1:
        return [
            Violation(
                rule="meal_plan_energy",
                message="a plan needs exactly one energy target and one plan-energy claim",
            )
        ]
    target, plan_claim = targets[0][1].value, claimed[0][1].value
    sex = rec.user.sex.lower()
    if sex not in ENERGY_FLOOR_KCAL:
        return [Violation(rule="meal_plan_energy", message=f"no energy floor for sex {sex!r}")]
    floor = ENERGY_FLOOR_KCAL[sex]
    totals: dict[object, float] = {}
    for e in rec.meal_plan:
        recipe = BY_ID.get(e.recipe_id)
        if recipe is None:
            continue  # reported by meal_plan_safe
        totals[e.day] = totals.get(e.day, 0.0) + recipe.kcal * e.scale
    for day, total in sorted(totals.items(), key=lambda kv: str(kv[0])):
        if not floor <= total <= ENERGY_CEILING_KCAL:
            out.append(
                Violation(
                    rule="meal_plan_energy",
                    message=f"{day}: {total:.0f} kcal is outside [{floor:.0f}, "
                    f"{ENERGY_CEILING_KCAL:.0f}]",
                )
            )
        if abs(total - target) > PLAN_TARGET_TOLERANCE * target:
            out.append(
                Violation(
                    rule="meal_plan_energy",
                    message=f"{day}: {total:.0f} kcal is more than "
                    f"{PLAN_TARGET_TOLERANCE:.0%} away from the target {target:.0f}",
                )
            )
    if totals:
        mean = sum(totals.values()) / len(totals)
        if abs(mean - plan_claim) > PLAN_TOTAL_EPSILON_KCAL:
            out.append(
                Violation(
                    rule="meal_plan_energy",
                    message=f"claimed average {plan_claim:.0f} kcal differs from the plan's "
                    f"{mean:.0f} kcal",
                )
            )
    return out


DEFAULT_RULES: tuple[Rule, ...] = (
    rule_has_claims,
    rule_no_unknown_provenance,
    rule_units_match,
    rule_energy_bounds,
    rule_intensity_cap,
    rule_sleep_bounds,
    rule_meal_plan_safe,
    rule_meal_plan_energy,
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
