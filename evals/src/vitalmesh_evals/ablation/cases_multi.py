"""Second ablation suite: cases where a multi-agent design should matter (D74).

The first suite (cases.py) is mostly single-topic questions, where one agent with tools has
nothing to be bad at. This suite asks for several domains at once (food, training, sleep, plus the
condition and lab review), and injects one outage to compare fault isolation. Written before any
run and not tuned afterwards; the same rules as cases.py apply (a change after seeing results is
disclosed in docs/DECISIONS.md).

The outage case is fault isolation BY CONSTRUCTION, and says so: in a single-agent system the one
agent IS every role, so its outage is total; in the graph one specialist's outage costs one part.
It demonstrates the architecture's claim; it is not evidence about answer quality.
"""

from __future__ import annotations

from pydantic import Field

from vitalmesh_evals.ablation.cases import (
    MEAL_WORDS,
    NO_CLINICAL,
    SLEEP_WORDS,
    TRAIN_WORDS,
    AblationCase,
    LabSpec,
    user,
)

SPECIALIST_PURPOSES = ("nutrition-specialist", "movement-specialist", "condition-specialist")


class MultiCase(AblationCase):
    """A case that may also inject an outage of the model behind some purposes, per arm."""

    outage: dict[str, tuple[str, ...]] = Field(default_factory=dict)  # arm -> purposes that fail


MULTI_CASES: tuple[MultiCase, ...] = (
    MultiCase(
        id="food-train-labs",
        note="three domains plus a lab review in one question",
        profile=user(goal="lose", weight_kg=84),
        question=(
            "I want to eat better and train for a 10k, and my LDL was high. "
            "What should I do about food, training and sleep?"
        ),
        labs=(LabSpec(name="LDL Cholesterol", value=128, unit="mg/dL", reference="< 100"),),
        cover=(MEAL_WORDS, TRAIN_WORDS, SLEEP_WORDS),
        forbid=(*NO_CLINICAL, r"\byou have (?:high cholesterol|hyperlipid)"),
    ),
    MultiCase(
        id="celiac-vegan-training",
        note="two dietary constraints and training in one request",
        profile=user(conditions=["celiac_disease"], diet="vegan", sex="male", weight_kg=76),
        question="Plan three days of meals and a training routine for me.",
        cover=(MEAL_WORDS, TRAIN_WORDS),
        forbid=(
            r"\bwheat\b",
            r"\bbread\b",
            r"\bpasta\b",
            r"\bnoodles?\b",
            r"\bchicken\b",
            r"\bfish\b",
            r"\begg",
        ),
    ),
    MultiCase(
        id="outage-one-agent",
        note="fault isolation: the model behind ONE agent role is down",
        profile=user(),
        question="Give me food, training and sleep advice for the week.",
        cover=(MEAL_WORDS,),
        outage={
            "single": ("single-specialist",),
            "t2": SPECIALIST_PURPOSES,
            "t3": ("movement-specialist",),
        },
    ),
)
