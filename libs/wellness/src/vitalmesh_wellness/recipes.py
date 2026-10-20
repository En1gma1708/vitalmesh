"""Recipe catalogue for the rule-driven meal planner (D52).

Synthetic and illustrative: dish names are generic and the energy and macro values are rounded
estimates written for this project, NOT taken from a food-composition database, and must not be
presented as nutritional data. Replace with an open dataset (and cite it) before any real use.

This module is the single source of truth for what a recipe contains. The planner picks from it
and the Guardian re-derives every check from it independently, so a recipe id that is not here
can never be released.

Allergen labels: ``nuts`` covers peanuts and tree nuts together (the conservative choice).
Diet levels are ordered: vegan < vegetarian (lacto-ovo) < omnivore.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict, Field

CATALOGUE_VERSION = "recipes-1"
PORTION_MIN = 0.5
PORTION_MAX = 2.0


class Meal(StrEnum):
    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    SNACK = "snack"


class Diet(IntEnum):
    VEGAN = 0
    VEGETARIAN = 1
    OMNIVORE = 2


ALLERGENS = frozenset({"nuts", "dairy", "gluten", "egg", "fish", "shellfish", "soy", "sesame"})
DIET_NAMES = {"vegan": Diet.VEGAN, "vegetarian": Diet.VEGETARIAN, "omnivore": Diet.OMNIVORE}


class Recipe(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    meal: Meal
    kcal: float = Field(gt=0)
    protein_g: float = Field(ge=0)
    carbs_g: float = Field(ge=0)
    fat_g: float = Field(ge=0)
    diet: Diet  # the strictest diet this recipe fits (vegan recipes fit everyone)
    allergens: frozenset[str] = frozenset()


def _r(
    id_: str,
    title: str,
    meal: Meal,
    kcal: float,
    p: float,
    c: float,
    f: float,
    diet: Diet,
    *allergens: str,
) -> Recipe:
    return Recipe(
        id=id_,
        title=title,
        meal=meal,
        kcal=kcal,
        protein_g=p,
        carbs_g=c,
        fat_g=f,
        diet=diet,
        allergens=frozenset(allergens),
    )


B, L, D, S = Meal.BREAKFAST, Meal.LUNCH, Meal.DINNER, Meal.SNACK
VG, VT, OM = Diet.VEGAN, Diet.VEGETARIAN, Diet.OMNIVORE

CATALOGUE: tuple[Recipe, ...] = (
    _r("b01", "Oat porridge with banana", B, 380, 10, 70, 6, VG, "gluten"),
    _r("b02", "Vegetable besan chilla", B, 320, 14, 42, 10, VG),
    _r("b03", "Scrambled eggs on toast", B, 400, 22, 30, 20, VT, "egg", "gluten"),
    _r("b04", "Yogurt with berries and almonds", B, 330, 20, 32, 14, VT, "dairy", "nuts"),
    _r("b05", "Poha with peanuts", B, 360, 9, 58, 10, VG, "nuts"),
    _r("b06", "Tofu scramble with spinach", B, 300, 24, 10, 18, VG, "soy"),
    _r("b07", "Smoked salmon bagel", B, 420, 24, 48, 14, OM, "fish", "gluten"),
    _r("l01", "Chickpea and vegetable curry with rice", L, 560, 18, 92, 12, VG),
    _r("l02", "Grilled chicken salad bowl with quinoa", L, 520, 42, 46, 16, OM),
    _r("l03", "Paneer wrap", L, 540, 24, 58, 24, VT, "dairy", "gluten"),
    _r("l04", "Lentil soup with bread", L, 480, 26, 74, 8, VG, "gluten"),
    _r("l05", "Tuna and bean salad", L, 450, 36, 40, 16, OM, "fish"),
    _r("l06", "Egg fried rice with vegetables", L, 520, 20, 76, 14, VT, "egg", "soy"),
    _r("l07", "Rajma with brown rice", L, 540, 20, 90, 9, VG),
    _r("d01", "Baked fish with roasted vegetables", D, 520, 38, 42, 20, OM, "fish"),
    _r("d02", "Tofu vegetable stir-fry with noodles", D, 560, 26, 70, 18, VG, "soy", "gluten"),
    _r("d03", "Dal, roti and mixed vegetables", D, 580, 22, 92, 14, VG, "gluten"),
    _r("d04", "Chicken curry with rice", D, 620, 40, 70, 20, OM),
    _r("d05", "Mushroom and spinach pasta with cheese", D, 600, 24, 82, 20, VT, "dairy", "gluten"),
    _r("d06", "Quinoa and roasted vegetable bowl with tahini", D, 540, 18, 70, 22, VG, "sesame"),
    _r("d07", "Prawn and vegetable rice bowl", D, 540, 32, 68, 14, OM, "shellfish"),
    _r("d08", "Baked sweet potato with black beans", D, 480, 16, 88, 6, VG),
    _r("s01", "Apple with peanut butter", S, 220, 6, 28, 10, VG, "nuts"),
    _r("s02", "Roasted chickpeas", S, 160, 8, 24, 4, VG),
    _r("s03", "Yogurt cup", S, 150, 10, 16, 5, VT, "dairy"),
    _r("s04", "Fruit salad", S, 120, 2, 28, 0.5, VG),
    _r("s05", "Two boiled eggs", S, 150, 12, 1, 10, VT, "egg"),
    _r("s06", "Hummus with carrot sticks", S, 180, 6, 18, 10, VG, "sesame"),
    _r("s07", "Small handful of mixed nuts", S, 200, 6, 8, 17, VG, "nuts"),
)

BY_ID: dict[str, Recipe] = {r.id: r for r in CATALOGUE}
