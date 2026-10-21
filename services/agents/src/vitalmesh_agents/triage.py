"""Triage/Budget: choose tier, specialists, risk and budget for a question, in code (D68).

Rules first: the plan is a pure function of the question text, facts about the user and the
current load, so every decision is explainable (``rule_ids``) and testable. The question is
untrusted DATA: it is matched against closed keyword lists and never interpreted by a model, so
no wording can change the plan beyond which closed-list words it contains. A model only helps
with questions the rules cannot place, and Phase 3 does not need one: an unplaced question is
treated as general wellness (nutrition plus movement), the broadest safe plan.

Risk is also decided here, from the user's facts, not from the model's output. Risk decides
whether the Checker (medium and up) and a human reviewer (high) are in the path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from vitalmesh_common.envelope import Tier
from vitalmesh_common.run import Risk, Specialist
from vitalmesh_wellness.knowledge import DEFAULT_INDEX, ConstraintIndex
from vitalmesh_wellness.labs import LabResult, Severity, severity

DEFAULT_MAX_MODEL_CALLS = 5
DEGRADED_MAX_MODEL_CALLS = 3
DEGRADED_MAX_SPECIALISTS = 2


class Load(StrEnum):
    """Back-pressure signal. Phase 4 derives it from lag and the LLM budget; until then it is
    an input so the degradation rule is real and tested."""

    NORMAL = "normal"
    HIGH = "high"  # fewer specialists and calls
    CRITICAL = "critical"  # a low-risk question is answered with no model at all (T0)


def _words(*terms: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(terms) + r")\b", re.IGNORECASE)


# Closed keyword lists. Illustrative product policy, not clinical content.
_NUTRITION = _words(
    "eat", "eating", "food", "foods", "meal", "meals", "diet", "calorie", "calories", "kcal",
    "protein", "carb", "carbs", "carbohydrate", "carbohydrates", "fat", "fats", "fibre", "fiber",
    "snack", "snacks", "breakfast", "lunch", "dinner", "recipe", "recipes", "nutrition",
    "sodium", "salt", "sugar", "vegan", "vegetarian", "hungry", "weight",
)  # fmt: skip
_MOVEMENT = _words(
    "workout", "workouts", "exercise", "exercising", "train", "training", "run", "running",
    "walk", "walking", "cardio", "strength", "lift", "lifting", "zone", "zones", "heart rate",
    "steps", "active", "activity", "sleep", "sleeping", "rest", "recovery", "gym", "stretch",
    "stretching", "fitness",
)  # fmt: skip
_CONDITION = _words(
    "condition", "conditions", "medication", "medications", "medicine", "pill", "pills", "drug",
    "drugs", "dose", "doses", "lab", "labs", "blood test", "blood work", "cholesterol", "ldl",
    "hdl", "glucose", "hba1c", "thyroid", "tsh", "creatinine", "triglycerides", "hemoglobin",
    "blood pressure", "hypertension", "diabetes", "diabetic", "cardiac", "asthma", "allergy",
    "allergic", "celiac",
)  # fmt: skip
_MEDICATION = _words(
    "medication", "medications", "medicine", "pill", "pills", "drug", "drugs", "dose", "doses",
    "prescription", "prescribed", "supplement", "supplements",
)  # fmt: skip


@dataclass(frozen=True)
class Signals:
    """Facts about the user that shape the plan. Built from the stores, never from the model."""

    conditions: frozenset[str] = frozenset()
    medications: frozenset[str] = frozenset()
    intensity_limited: bool = False
    abnormal_labs: tuple[str, ...] = ()
    marked_labs: tuple[str, ...] = ()

    @property
    def has_conditions(self) -> bool:
        return bool(self.conditions or self.medications)


def signals_for(
    conditions: frozenset[str],
    medications: frozenset[str],
    labs: list[LabResult],
    index: ConstraintIndex = DEFAULT_INDEX,
) -> Signals:
    """Latest result per marker decides: an old abnormal value that was re-tested normal does
    not raise the risk."""
    latest: dict[str, LabResult] = {}
    for r in sorted(labs, key=lambda r: r.collected_at):
        latest[r.name.strip().lower()] = r
    abnormal: list[str] = []
    marked: list[str] = []
    for r in latest.values():
        sev = severity(r)
        if sev is Severity.MARKED:
            marked.append(r.name)
        if sev in (Severity.ABNORMAL, Severity.MARKED):
            abnormal.append(r.name)
    limiting = index.limiting_conditions()
    return Signals(
        conditions=frozenset(c.strip().lower() for c in conditions),
        medications=frozenset(m.strip().lower() for m in medications),
        intensity_limited=any(c.strip().lower() in limiting for c in conditions),
        abnormal_labs=tuple(sorted(abnormal)),
        marked_labs=tuple(sorted(marked)),
    )


@dataclass(frozen=True)
class Plan:
    tier: Tier
    specialists: tuple[Specialist, ...]
    risk: Risk
    max_model_calls: int
    degraded: bool
    rule_ids: tuple[str, ...] = field(default_factory=tuple)


def plan_run(question: str, signals: Signals, load: Load = Load.NORMAL) -> Plan:
    rules: list[str] = []
    chosen: list[Specialist] = []

    if _NUTRITION.search(question):
        chosen.append(Specialist.NUTRITION)
        rules.append("route.nutrition_terms")
    if _MOVEMENT.search(question):
        chosen.append(Specialist.MOVEMENT_RECOVERY)
        rules.append("route.movement_terms")
    asks_condition = bool(_CONDITION.search(question))
    if asks_condition:
        chosen.append(Specialist.CONDITION_MEDICATION)
        rules.append("route.condition_terms")

    if not chosen:
        chosen = [Specialist.NUTRITION, Specialist.MOVEMENT_RECOVERY]
        rules.append("route.general_wellness")

    # Advice to someone with declared conditions, medications or flagged labs always gets the
    # condition review, whatever they asked: those are safety constraints (AGENTS.md section 2).
    needs_review = signals.has_conditions or bool(signals.abnormal_labs)
    if needs_review and Specialist.CONDITION_MEDICATION not in chosen:
        chosen.append(Specialist.CONDITION_MEDICATION)
        rules.append("review.profile_constraints")

    risk = _risk(question, signals, tuple(chosen), rules)

    degraded = False
    max_calls = DEFAULT_MAX_MODEL_CALLS
    if load is not Load.NORMAL:
        degraded = True
        max_calls = DEGRADED_MAX_MODEL_CALLS
        rules.append("load.degraded")
        if len(chosen) > DEGRADED_MAX_SPECIALISTS:
            # keep the condition review when risk needs it; drop from the end otherwise
            keep_condition = Specialist.CONDITION_MEDICATION in chosen and risk is not Risk.LOW
            if keep_condition:
                others = [s for s in chosen if s is not Specialist.CONDITION_MEDICATION]
                chosen = [*others[: DEGRADED_MAX_SPECIALISTS - 1], Specialist.CONDITION_MEDICATION]
            else:
                chosen = chosen[:DEGRADED_MAX_SPECIALISTS]
            rules.append("load.fewer_specialists")

    tier = Tier.T2 if len(chosen) == 1 and risk is Risk.LOW else Tier.T3
    # Shedding is only for questions whose safe answer is the user's own calculators: low risk,
    # no condition review. Anything riskier keeps its checks and is merely degraded.
    if load is Load.CRITICAL and risk is Risk.LOW and Specialist.CONDITION_MEDICATION not in chosen:
        tier = Tier.T0
        rules.append("load.shed_to_t0")
    return Plan(
        tier=tier,
        specialists=tuple(chosen),
        risk=risk,
        max_model_calls=max_calls,
        degraded=degraded,
        rule_ids=tuple(rules),
    )


def _risk(
    question: str, signals: Signals, chosen: tuple[Specialist, ...], rules: list[str]
) -> Risk:
    # High: a human should see it before release.
    if signals.marked_labs and Specialist.CONDITION_MEDICATION in chosen:
        rules.append("risk.marked_lab_value")
        return Risk.HIGH
    if signals.intensity_limited and Specialist.MOVEMENT_RECOVERY in chosen:
        rules.append("risk.exercise_with_limiting_condition")
        return Risk.HIGH
    if _MEDICATION.search(question):
        rules.append("risk.medication_question")
        return Risk.HIGH
    # Medium: the Checker reviews it.
    if signals.has_conditions or signals.abnormal_labs:
        rules.append("risk.profile_constraints")
        return Risk.MEDIUM
    if len(chosen) >= 3:
        rules.append("risk.many_specialists")
        return Risk.MEDIUM
    return Risk.LOW
