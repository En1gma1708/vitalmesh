"""Sentences the answer must carry whatever the writer model said (D108)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from vitalmesh.events.v1 import common_pb2, payloads_pb2
from vitalmesh_agents.required_notes import (
    BMI_CAVEAT,
    EXTREME_NOTE,
    UNDERWEIGHT_NOTE,
    required_sentences,
    with_required_sentences,
)
from vitalmesh_agents.synthesizer_handler import _draft
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.run import Risk


def claim(metric: str, value: float) -> payloads_pb2.RecommendationClaim:
    c = payloads_pb2.RecommendationClaim(
        text=f"{metric} {value}",
        provenance=common_pb2.PROVENANCE_COMPUTED,
        source="t",
        metric=metric,
    )
    c.quantity.value, c.quantity.unit = value, "kg/m2"
    return c


def test_no_bmi_claim_means_nothing_is_added() -> None:
    assert required_sentences("Aim for 2000 kcal.", [claim("daily_energy_target", 2000)]) == []


def test_a_bmi_statement_always_carries_the_caveat() -> None:
    assert required_sentences("Your BMI is 27.9.", [claim("bmi", 27.9)]) == [BMI_CAVEAT]
    assert required_sentences("BMI ignores muscle, age and pregnancy.", [claim("bmi", 27.9)]) == []


@pytest.mark.parametrize(
    ("bmi", "extra"),
    [
        (17.9, [UNDERWEIGHT_NOTE]),
        (18.5, []),
        (15.9, [EXTREME_NOTE]),
        (39.9, []),
        (40.0, [EXTREME_NOTE]),
    ],
)
def test_the_doctor_lines_follow_the_bmi_exactly(bmi: float, extra: list[str]) -> None:
    assert required_sentences("Your BMI.", [claim("bmi", bmi)]) == [BMI_CAVEAT, *extra]


def test_a_doctor_line_already_in_the_text_is_not_repeated() -> None:
    text = "Please talk to a doctor. BMI ignores muscle, age and pregnancy."
    assert required_sentences(text, [claim("bmi", 17.0)]) == []


def test_the_sentences_carry_no_figure_the_gate_could_block() -> None:
    for s in (BMI_CAVEAT, UNDERWEIGHT_NOTE, EXTREME_NOTE):
        assert not any(ch.isdigit() for ch in s)


def test_the_draft_gets_them_by_code() -> None:
    meta = EventMeta(
        correlation_id="c", user_id="u", household_id="h", type="advice.bundle",
        occurred_at=datetime(2026, 11, 11, tzinfo=UTC),
    )  # fmt: skip
    draft = _draft(
        meta, "what is my bmi", Risk.LOW, "Your BMI is 27.9.", False,
        [claim("bmi", 27.9)], [], False, [], revision=0,
    )  # fmt: skip
    assert draft.text == f"Your BMI is 27.9. {BMI_CAVEAT}"
    assert with_required_sentences("x", []) == "x"
