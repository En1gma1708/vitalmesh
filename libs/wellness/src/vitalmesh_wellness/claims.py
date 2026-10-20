"""Typed claims with provenance (docs/ARCHITECTURE.md section 12, principle 1).

Every statement in a recommendation is ``computed`` (a named calculator produced it),
``cited`` (a guideline source supports it), ``inferred`` (a reasoned step, with its basis) or
``unknown``. ``unknown`` is representable on purpose so the Guardian can block it; it is never
treated as safe. Provenance fields are validated at construction, so a "computed" claim with
no calculator cannot exist.
"""

from __future__ import annotations

import math
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Provenance(StrEnum):
    COMPUTED = "computed"
    CITED = "cited"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class Quantity(BaseModel):
    """A number with an explicit unit and a registered metric name (never an implied unit)."""

    model_config = ConfigDict(frozen=True)

    metric: str = Field(min_length=1)
    value: float
    unit: str = Field(min_length=1)

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("quantity value must be finite")
        return v


class Claim(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1)
    provenance: Provenance
    # computed: calculator name+version; cited: citation id; inferred: the basis it rests on.
    source: str | None = None
    quantity: Quantity | None = None

    @model_validator(mode="after")
    def _provenance_is_backed(self) -> Claim:
        needs_source = self.provenance in (
            Provenance.COMPUTED,
            Provenance.CITED,
            Provenance.INFERRED,
        )
        if needs_source and not (self.source and self.source.strip()):
            raise ValueError(f"a {self.provenance.value!r} claim needs a source")
        if self.provenance is Provenance.COMPUTED and self.quantity is None:
            raise ValueError("a 'computed' claim must carry the quantity it computed")
        return self


class UserConstraints(BaseModel):
    """What the Guardian needs to know about the user. Conditions are safety constraints."""

    model_config = ConfigDict(frozen=True)

    user_id: str = Field(min_length=1)
    sex: str
    age_years: float
    conditions: frozenset[str] = frozenset()
    medications: frozenset[str] = frozenset()
    diet: str = "omnivore"  # omnivore / vegetarian / vegan; anything else blocks a meal plan
    allergens: frozenset[str] = frozenset()

    @field_validator("allergens", "conditions", "medications")
    @classmethod
    def _folded(cls, v: frozenset[str]) -> frozenset[str]:
        return frozenset(x.strip().lower() for x in v if x.strip())


class MealEntry(BaseModel):
    """One planned meal. Only identifiers and a portion: everything checkable (allergens, diet,
    energy) is looked up in the recipe catalogue by the Guardian, never taken from the planner."""

    model_config = ConfigDict(frozen=True)

    day: date
    meal: str = Field(min_length=1)
    recipe_id: str = Field(min_length=1)
    scale: float

    @field_validator("scale")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("scale must be finite")
        return v


class Recommendation(BaseModel):
    model_config = ConfigDict(frozen=True)

    recommendation_id: str = Field(min_length=1)
    user: UserConstraints
    claims: tuple[Claim, ...]
    meal_plan: tuple[MealEntry, ...] = ()
