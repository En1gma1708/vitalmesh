"""Sentences the answer must carry whatever the writer model chose to say (D108). Pure.

The writer may shorten the helper's notes, and a live reading showed it dropping the BMI caveat in
four of five BMI answers. What must never depend on a model's mood is decided from the CLAIMS, in
code, and appended when the text lacks it. Every sentence here has no figure of its own, so the
Guardian's "no stray number" rule can never block it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from vitalmesh_wellness.bodystatus import BMI_UNDERWEIGHT

BMI_CAVEAT = "BMI is a rough guide: it ignores muscle, age and pregnancy."
UNDERWEIGHT_NOTE = (
    "Your BMI is below the healthy range, so I won't suggest losing weight. "
    "Please talk to a doctor or a registered dietitian."
)
EXTREME_NOTE = (
    "At this BMI a weight plan needs a doctor, so I don't suggest a target or a pace. "
    "Please talk to a doctor or a registered dietitian."
)
EXTREME_LOW = 16.0
EXTREME_HIGH = 40.0


def bmi_values(claims: Sequence[Any]) -> list[float]:
    return [
        float(c.quantity.value)
        for c in claims
        if getattr(c, "metric", "") == "bmi" and c.HasField("quantity")
    ]


def required_sentences(text: str, claims: Sequence[Any]) -> list[str]:
    """The fixed sentences the text lacks, in the order they should be appended."""
    values = bmi_values(claims)
    if not values:
        return []
    low = text.lower()
    out: list[str] = []
    if "ignores muscle" not in low:
        out.append(BMI_CAVEAT)
    bmi = values[0]
    if (bmi < EXTREME_LOW or bmi >= EXTREME_HIGH) and "needs a doctor" not in low:
        out.append(EXTREME_NOTE)
    elif BMI_UNDERWEIGHT > bmi >= EXTREME_LOW and "talk to a doctor" not in low:
        out.append(UNDERWEIGHT_NOTE)
    return out


def with_required_sentences(text: str, claims: Sequence[Any]) -> str:
    extra = required_sentences(text, claims)
    return " ".join([text.rstrip(), *extra]) if extra else text
