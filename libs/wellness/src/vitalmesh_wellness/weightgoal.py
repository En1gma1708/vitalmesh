"""Unsafe weight requests (D108, step one): pure code, no model, no I/O.

A question that asks to reach a weight below the healthy range for the person's height, or to
lose weight faster than a safe rate, is declined with a supportive message. The thresholds are
written here, not in a prompt:

* a target weight is unsafe when it gives a BMI under 18.5 (the WHO adult underweight line) for
  the person's height; with no height known, a target under 40 kg is unsafe whatever the height;
* a loss is unsafe when it is faster than 1 kg a week or 1 percent of the starting body weight a
  week, whichever is smaller (a common upper bound for a safe rate);
* "how low can I go" style questions are declined: there is no safe answer without a clinician.

BMI is a crude screen (muscle, age, ethnicity, pregnancy), which is why this module only ever
DECLINES an extreme request; it never produces a target of its own.
"""

from __future__ import annotations

import re
import unicodedata

UNDERWEIGHT_BMI = 18.5
ABSOLUTE_FLOOR_KG = 40.0  # used only when the height is unknown
MAX_LOSS_KG_PER_WEEK = 1.0
MAX_LOSS_FRACTION_PER_WEEK = 0.01

SUPPORTIVE_REFUSAL = (
    "I can't plan toward that: it is below a healthy range or faster than is safe, and that can "
    "harm you. If you are worried about your weight or how you feel about food, please talk to a "
    "doctor or a registered dietitian. I can help you work out a healthy range and a gradual, "
    "safe pace instead."
)

_KG = r"(\d{2,3}(?:\.\d+)?)\s*(kgs?|kilos?|kilograms?)"
_LB = r"(\d{2,3}(?:\.\d+)?)\s*(lbs?|pounds?)"
_KG_LOSS = r"(\d{1,3}(?:\.\d+)?)\s*(kgs?|kilos?|kilograms?)"
_LB_LOSS = r"(\d{1,3}(?:\.\d+)?)\s*(lbs?|pounds?)"
_TARGET = re.compile(
    r"\b(?:weigh|weighing|reach|get to|get down to|down to|be|become|hit|"
    r"target(?: weight)?(?: of)?)"
    rf"\s*(?:a |an |just |only |about |around )?(?:weight of )?(?:{_KG}|{_LB})"
)
_LOSS = re.compile(
    rf"\b(?:lose|drop|shed|cut)\s*(?:about |around |up to |at least )?(?:{_KG_LOSS}|{_LB_LOSS})"
    rf"(?:\s*(?:a|per|each|every|in|within|in a|in one|in the next)\s*(?:the next )?"
    rf"(?:(\d+)\s*)?(day|days|week|weeks|month|months))?"
)
_HOW_LOW = re.compile(
    r"\bhow (?:low|little|light)\b.{0,25}\b(?:go|weigh|get)\b|\blowest (?:weight|i can)\b"
)


def _norm(question: str) -> str:
    return unicodedata.normalize("NFKC", question).casefold()


def _to_kg(value: float, unit: str) -> float:
    return value * 0.45359237 if unit.startswith(("lb", "pound")) else value


def target_weight_kg(question: str) -> float | None:
    """The weight in kg the person says they want to reach, if the question states one."""
    m = _TARGET.search(_norm(question))
    if m is None:
        return None
    if m.group(1) is not None:
        return float(m.group(1))
    return _to_kg(float(m.group(3)), m.group(4))


def loss_per_week_kg(question: str) -> float | None:
    """The stated loss pace in kg a week; None when no amount AND period are both stated."""
    m = _LOSS.search(_norm(question))
    if m is None or m.group(6) is None:
        return None
    amount = float(m.group(1)) if m.group(1) is not None else _to_kg(float(m.group(3)), m.group(4))
    count = float(m.group(5)) if m.group(5) else 1.0
    unit = m.group(6)
    days = {"day": 1.0, "week": 7.0, "month": 30.0}[unit.rstrip("s")] * count
    return amount / days * 7.0


def bmi(weight_kg: float, height_cm: float) -> float:
    return weight_kg / (height_cm / 100.0) ** 2


def unsafe_weight_request(
    question: str, *, height_cm: float | None = None, weight_kg: float | None = None
) -> str | None:
    """A reason code when the question asks for an unsafe target or pace, else None.

    Codes: ``target_below_healthy``, ``loss_too_fast``, ``how_low``.
    """
    text = _norm(question)
    if _HOW_LOW.search(text):
        return "how_low"
    target = target_weight_kg(question)
    if target is not None:
        if height_cm is not None:
            if bmi(target, height_cm) < UNDERWEIGHT_BMI:
                return "target_below_healthy"
        elif target < ABSOLUTE_FLOOR_KG:
            return "target_below_healthy"
    rate = loss_per_week_kg(question)
    if rate is not None:
        cap = MAX_LOSS_KG_PER_WEEK
        if weight_kg is not None:
            cap = min(cap, MAX_LOSS_FRACTION_PER_WEEK * weight_kg)
        if rate > cap:
            return "loss_too_fast"
    return None
