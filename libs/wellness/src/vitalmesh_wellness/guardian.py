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

from vitalmesh_wellness.bodystatus import BMI_UNDERWEIGHT
from vitalmesh_wellness.claims import Claim, Provenance, Quantity, Recommendation
from vitalmesh_wellness.knowledge import DEFAULT_INDEX
from vitalmesh_wellness.recipes import ALLERGENS, BY_ID, DIET_NAMES, PORTION_MAX, PORTION_MIN
from vitalmesh_wellness.weightgoal import MAX_LOSS_FRACTION_PER_WEEK, MAX_LOSS_KG_PER_WEEK
from vitalmesh_wellness.workouts import (
    BEGINNER_MAX_ZONE,
    MAX_EXERCISES,
    MAX_MINUTES,
    MAX_SETS_PER_EXERCISE,
    MAX_TOTAL_SETS,
    MIN_MINUTES,
    strength_allowed,
)
from vitalmesh_wellness.workouts import BY_ID as EXERCISES

RULES_VERSION = "5"

# Metric names the rules understand, with the only unit each may use.
METRIC_ENERGY = "daily_energy_target"
METRIC_HR_ZONE = "target_hr_zone"
METRIC_SLEEP = "sleep_target"
METRIC_PLAN_ENERGY = "meal_plan_avg_day_energy"
METRIC_WORKOUT_MINUTES = "workout_minutes"
METRIC_WORKOUT_ZONE = "workout_cardio_zone"
METRIC_WORKOUT_EXERCISE_SETS = "workout_exercise_sets"
METRIC_WORKOUT_TOTAL_SETS = "workout_total_sets"
METRIC_BMI = "bmi"
METRIC_BODY_WEIGHT = "body_weight"
METRIC_HEALTHY_LOW = "healthy_weight_low"
METRIC_HEALTHY_HIGH = "healthy_weight_high"
METRIC_WEIGHT_MILESTONE = "weight_milestone"
METRIC_MILESTONE_BMI = "weight_milestone_bmi"
METRIC_LOSS_RATE = "weight_loss_rate"
METRIC_LOSS_RATE_PCT = "weight_loss_rate_pct"
EXPECTED_UNIT = {
    METRIC_BMI: "kg/m2",
    METRIC_BODY_WEIGHT: "kg",
    METRIC_HEALTHY_LOW: "kg",
    METRIC_HEALTHY_HIGH: "kg",
    METRIC_WEIGHT_MILESTONE: "kg",
    METRIC_MILESTONE_BMI: "kg/m2",
    METRIC_LOSS_RATE: "kg/week",
    METRIC_LOSS_RATE_PCT: "%/week",
    METRIC_ENERGY: "kcal",
    METRIC_HR_ZONE: "zone",
    METRIC_SLEEP: "h",
    METRIC_PLAN_ENERGY: "kcal",
    METRIC_WORKOUT_MINUTES: "min",
    METRIC_WORKOUT_ZONE: "zone",
    METRIC_WORKOUT_EXERCISE_SETS: "sets",
    METRIC_WORKOUT_TOTAL_SETS: "sets",
}
PLAN_TARGET_TOLERANCE = 0.15  # each planned day must land within 15% of the energy target
PLAN_TOTAL_EPSILON_KCAL = 0.5

BMI_PLAN_REVIEW_BELOW = 16.0  # a weight plan at a BMI this low or at 40 and over needs a clinician
BMI_PLAN_REVIEW_FROM = 40.0
ENERGY_FLOOR_KCAL = {"female": 1200.0, "male": 1500.0}
ENERGY_CEILING_KCAL = 6000.0
SLEEP_TARGET_HOURS = (7.0, 10.0)
# Conditions that cap exercise intensity at zone 3 until a clinician clears more.
INTENSITY_LIMITING_CONDITIONS = DEFAULT_INDEX.limiting_conditions()  # from the knowledge graph


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
    cap = DEFAULT_INDEX.max_zone(rec.user.conditions)
    if cap is None:
        return []
    return [
        Violation(
            rule="intensity_cap",
            message=f"zone {q.value:g} exceeds zone {cap:g} "
            "for a user with an intensity-limiting condition",
            claim_text=c.text,
        )
        for metric in (METRIC_HR_ZONE, METRIC_WORKOUT_ZONE)
        for c, q in _quantities(rec, metric)
        if q.value > cap
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
    avoided = user.effective_allergens
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
        hit = recipe.allergens & avoided
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


def rule_workout_safe(rec: Recommendation) -> list[Violation]:
    """Bounds on the workout claims, and every planned exercise re-checked against the catalogue.

    Strength work needs every declared condition to be one the knowledge graph knows and that does
    not limit intensity; an unknown condition is never assumed safe. In the T3 graph only claims
    reach this rule, so their bounds and the condition check apply there; the entry-level checks
    apply wherever the plan itself is present (the movement tool evaluates it before answering).
    """
    exercise_claims = _quantities(rec, METRIC_WORKOUT_EXERCISE_SETS)
    totals = _quantities(rec, METRIC_WORKOUT_TOTAL_SETS)
    minutes = _quantities(rec, METRIC_WORKOUT_MINUTES)
    zones = _quantities(rec, METRIC_WORKOUT_ZONE)
    strength = bool(exercise_claims or totals or rec.workout_plan)
    if not (strength or minutes or zones):
        return []
    out: list[Violation] = []

    def block(message: str, claim: Claim | None = None) -> None:
        out.append(
            Violation(
                rule="workout_safe", message=message, claim_text=claim.text if claim else None
            )
        )

    if strength and not strength_allowed(rec.user.conditions):
        block("strength work is not allowed for the conditions this user declared")
    for c, q in minutes:
        if not MIN_MINUTES <= q.value <= MAX_MINUTES:
            block(f"workout of {q.value:g} min is outside [{MIN_MINUTES}, {MAX_MINUTES}]", c)
    for c, q in zones:
        if not 1 <= q.value <= BEGINNER_MAX_ZONE:
            block(f"workout zone {q.value:g} is outside [1, {BEGINNER_MAX_ZONE}]", c)
    for c, q in exercise_claims:
        if not (1 <= q.value <= MAX_SETS_PER_EXERCISE and q.value == int(q.value)):
            block(f"{q.value:g} sets is outside [1, {MAX_SETS_PER_EXERCISE}] sets", c)
    if len(exercise_claims) > MAX_EXERCISES:
        block(f"{len(exercise_claims)} exercises is more than {MAX_EXERCISES}")
    claimed_total: float | None = None
    if strength:
        if len(totals) != 1:
            block("a workout needs exactly one total-sets claim")
        else:
            claimed_total = totals[0][1].value
            if claimed_total > MAX_TOTAL_SETS:
                block(f"{claimed_total:g} total sets is more than {MAX_TOTAL_SETS}", totals[0][0])
            if exercise_claims:
                listed = sum(q.value for _, q in exercise_claims)
                if listed != claimed_total:  # whole numbers: exact is right
                    block(f"the exercises add up to {listed:g} sets, not {claimed_total:g}")
    for e in rec.workout_plan:
        label = f"{e.day} {e.exercise_id}"
        ex = EXERCISES.get(e.exercise_id)
        if ex is None:
            block(f"unknown exercise ({label})")
            continue
        if not 1 <= e.sets <= MAX_SETS_PER_EXERCISE:
            block(f"sets out of range ({label})")
        if not ex.reps_low <= e.reps_low <= e.reps_high <= ex.reps_high:
            block(f"reps out of range ({label})")
    if rec.workout_plan:
        planned = sum(e.sets for e in rec.workout_plan)
        if len({e.day for e in rec.workout_plan}) > 1:
            block("a workout covers one day")
        if len(rec.workout_plan) > MAX_EXERCISES or planned > MAX_TOTAL_SETS:
            block("too much volume for one workout")
        if claimed_total is not None and planned != claimed_total:
            block(f"the plan has {planned} sets, the claim says {claimed_total:g}")
    return out


def rule_weight_target_safe(rec: Recommendation) -> list[Violation]:
    """A target weight must carry its BMI, and that BMI must not be under the underweight line.

    A milestone claim without its BMI claim cannot be checked, so it blocks (fail closed)."""
    out: list[Violation] = []
    milestones = _quantities(rec, METRIC_WEIGHT_MILESTONE)
    bmis = _quantities(rec, METRIC_MILESTONE_BMI)
    if milestones and not bmis:
        out.append(
            Violation(
                rule="weight_target_safe",
                message="a target weight must come with the BMI it gives, so it can be checked",
                claim_text=milestones[0][0].text,
            )
        )
    for c, q in bmis:
        if q.value < BMI_UNDERWEIGHT:
            out.append(
                Violation(
                    rule="weight_target_safe",
                    message=f"a target weight giving a BMI of {q.value:.1f} is under "
                    f"{BMI_UNDERWEIGHT} (underweight)",
                    claim_text=c.text,
                )
            )
    return out


def rule_weight_loss_rate_safe(rec: Recommendation) -> list[Violation]:
    """No loss pace above 1 kg a week or 1 percent of body weight a week."""
    out: list[Violation] = []
    for c, q in _quantities(rec, METRIC_LOSS_RATE):
        if q.value > MAX_LOSS_KG_PER_WEEK:
            out.append(
                Violation(
                    rule="weight_loss_rate_safe",
                    message=f"{q.value:g} kg a week is faster than {MAX_LOSS_KG_PER_WEEK:g} kg",
                    claim_text=c.text,
                )
            )
    for c, q in _quantities(rec, METRIC_LOSS_RATE_PCT):
        if q.value > MAX_LOSS_FRACTION_PER_WEEK * 100:
            out.append(
                Violation(
                    rule="weight_loss_rate_safe",
                    message=f"{q.value:g} percent of body weight a week is faster than "
                    f"{MAX_LOSS_FRACTION_PER_WEEK * 100:g} percent",
                    claim_text=c.text,
                )
            )
    return out


def rule_bmi_extreme_review(rec: Recommendation) -> list[Violation]:
    """A BMI under 16 or at 40 and over may be stated, but never with a weight plan: that needs a
    clinician, not an automated target or pace."""
    plan = bool(_quantities(rec, METRIC_WEIGHT_MILESTONE) or _quantities(rec, METRIC_LOSS_RATE))
    if not plan:
        return []
    return [
        Violation(
            rule="bmi_extreme_review",
            message=f"a weight plan at a BMI of {q.value:.1f} needs a clinician, not a target",
            claim_text=c.text,
        )
        for c, q in _quantities(rec, METRIC_BMI)
        if q.value < BMI_PLAN_REVIEW_BELOW or q.value >= BMI_PLAN_REVIEW_FROM
    ]


DEFAULT_RULES: tuple[Rule, ...] = (
    rule_has_claims,
    rule_no_unknown_provenance,
    rule_units_match,
    rule_energy_bounds,
    rule_intensity_cap,
    rule_sleep_bounds,
    rule_meal_plan_safe,
    rule_meal_plan_energy,
    rule_workout_safe,
    rule_weight_target_safe,
    rule_weight_loss_rate_safe,
    rule_bmi_extreme_review,
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
