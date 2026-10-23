"""The onboarding profile a person submits, with closed lists (D75).

Every choice that the safety rules read (diet, allergens, conditions, medications) is a fixed list,
never free text: a typo in a free-text allergy would silently mean "no allergy". Numbers are
bounded to plausible adult values; the product is for adults and says so. Conditions come from the
set the knowledge graph actually has rules for; a condition we have no rules for is not offered
(the user is told to ask a clinician) rather than accepted and ignored. Medications are classes,
never brand names or doses: the system never advises on medication (D40), it only needs to know
that one is taken so the conditions review always runs.

``consent`` must be true and names the notice version the person saw.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from vitalmesh_stores import ProfileRecord
from vitalmesh_wellness.engine import Profile

CONSENT_VERSION = "2026-10-v1"
Sex = Literal["female", "male"]
Activity = Literal["sedentary", "light", "moderate", "active", "very_active"]
Goal = Literal["maintain", "lose", "gain"]
Diet = Literal["omnivore", "vegetarian", "vegan"]
Allergen = Literal["dairy", "egg", "fish", "gluten", "nuts", "sesame", "shellfish", "soy"]
Condition = Literal["cardiac", "celiac_disease", "lactose_intolerance", "uncontrolled_hypertension"]
Medication = Literal[
    "blood_pressure_medication",
    "diabetes_medication",
    "cholesterol_medication",
    "blood_thinner",
    "thyroid_medication",
    "other_prescription",
]

# What the onboarding screen offers, in the order it shows them (the page reads this).
CHOICES: dict[str, list[str]] = {
    "sex": ["female", "male"],
    "activity": ["sedentary", "light", "moderate", "active", "very_active"],
    "goal": ["maintain", "lose", "gain"],
    "diet": ["omnivore", "vegetarian", "vegan"],
    "allergens": ["dairy", "egg", "fish", "gluten", "nuts", "sesame", "shellfish", "soy"],
    "conditions": ["cardiac", "celiac_disease", "lactose_intolerance", "uncontrolled_hypertension"],
    "medications": [
        "blood_pressure_medication",
        "diabetes_medication",
        "cholesterol_medication",
        "blood_thinner",
        "thyroid_medication",
        "other_prescription",
    ],
}


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sex: Sex
    age_years: Annotated[int, Field(ge=18, le=100)]
    weight_kg: Annotated[float, Field(ge=30, le=300)]
    height_cm: Annotated[float, Field(ge=120, le=230)]
    activity: Activity
    resting_hr_bpm: Annotated[float, Field(ge=35, le=120)]
    goal: Goal = "maintain"
    diet: Diet = "omnivore"
    allergens: list[Allergen] = Field(default_factory=list, max_length=8)
    conditions: list[Condition] = Field(default_factory=list, max_length=4)
    medications: list[Medication] = Field(default_factory=list, max_length=6)
    consent: bool
    consent_version: str

    @field_validator("consent")
    @classmethod
    def _consented(cls, v: bool) -> bool:
        if not v:
            raise ValueError("you need to accept the notice to continue")
        return v

    @field_validator("consent_version")
    @classmethod
    def _current_notice(cls, v: str) -> str:
        if v != CONSENT_VERSION:
            raise ValueError("the notice has changed; reload the page and read it again")
        return v

    def to_record(self, user_id: str) -> ProfileRecord:
        profile = Profile.model_validate(
            {
                "user_id": user_id,
                "sex": self.sex,
                "age_years": self.age_years,
                "weight_kg": self.weight_kg,
                "height_cm": self.height_cm,
                "activity": self.activity,
                "resting_hr_bpm": self.resting_hr_bpm,
                "goal": self.goal,
                "diet": self.diet,
                "allergens": frozenset(self.allergens),
                "conditions": frozenset(self.conditions),
                "medications": frozenset(self.medications),
            }
        )
        # no sleep history yet: the neutral 7 hours until the person logs real nights
        return ProfileRecord(profile=profile, recent_sleep_hours=tuple([7.0] * 14))


def stored_form(p: ProfileIn) -> dict[str, object]:
    """What goes in the durable store: the answers, not the consent flag's transport fields."""
    return p.model_dump(mode="json")
