from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from vitalmesh_evidence.types import AnswerSentence, Citation, EvidenceAnswer
from vitalmesh_specialists.tools import SPECS, NutritionTools
from vitalmesh_wellness.engine import Profile
from vitalmesh_wellness.guardian import Guardian

PROFILE = Profile.model_validate(
    {
        "user_id": "t",
        "sex": "female",
        "age_years": 30,
        "weight_kg": 65,
        "height_cm": 165,
        "activity": "moderate",
        "resting_hr_bpm": 60,
        "goal": "maintain",
        "diet": "vegetarian",
        "allergens": ["nuts"],
    }
)
SLEEP = [8.0] * 14


class FakeEvidence:
    def __init__(self, answer: EvidenceAnswer) -> None:
        self.answer_ = answer
        self.questions: list[str] = []

    def answer(self, question: str) -> EvidenceAnswer:
        self.questions.append(question)
        return self.answer_


SUPPORTED = EvidenceAnswer(
    status="supported",
    sentences=(
        AnswerSentence(
            text="Sodium should be under 2,300 mg a day.",
            citation=Citation(
                doc_id="dga-2020-2025",
                locator="p. 13",
                title="DGA",
                source_url="https://x.org",
                licence="public domain",
                quote="Sodium: less than 2,300 milligrams per day",
            ),
        ),
    ),
)


def tools(evidence: FakeEvidence | None = None, guardian: Guardian | None = None) -> NutritionTools:
    ev = evidence or FakeEvidence(EvidenceAnswer(status="insufficient_evidence"))
    return NutritionTools(PROFILE, SLEEP, ev, date(2026, 10, 26), guardian)


def test_specs_are_valid_json_schemas_and_names_match_the_dispatcher() -> None:
    assert [s.name for s in SPECS] == ["get_energy_target", "get_meal_plan", "search_guidelines"]
    for s in SPECS:
        assert s.parameters["type"] == "object"
        assert tools().call(
            s.name, "{}" if s.name != "search_guidelines" else '{"question": "salt?"}'
        )


def test_the_energy_tool_returns_the_engines_computed_claims() -> None:
    r = tools().call("get_energy_target", "")
    assert r.ok
    by_metric = {c["metric"]: c for c in r.payload["claims"]}
    assert by_metric["daily_energy_target"]["unit"] == "kcal"
    assert by_metric["daily_energy_target"]["value"] == pytest.approx(2123.9, abs=1)  # hand-checked
    assert json.loads(r.to_message_content()) == r.payload


def test_the_meal_plan_tool_returns_guardian_cleared_days_that_respect_the_profile() -> None:
    r = tools().call("get_meal_plan", '{"days": 2}')
    assert r.ok and len(r.payload["days"]) == 2
    for day in r.payload["days"]:
        assert day["total_kcal"] == sum(m["kcal"] for m in day["meals"]) > 1000
        assert {m["meal"] for m in day["meals"]} == {"breakfast", "lunch", "dinner", "snack"}
    default = tools().call("get_meal_plan", "{}")
    assert default.ok and len(default.payload["days"]) == 3


def test_a_blocked_recommendation_returns_an_error_and_no_numbers() -> None:
    def always_violates(rec: Any) -> list[Any]:
        raise RuntimeError("rule crashed")  # a rule that cannot run blocks (fail closed)

    g = Guardian(rules=(always_violates,))
    for name, args in (("get_energy_target", "{}"), ("get_meal_plan", "{}")):
        r = tools(guardian=g).call(name, args)
        assert not r.ok and r.payload == {} and "blocked" in r.error
        assert json.loads(r.to_message_content()) == {"error": r.error}


@pytest.mark.parametrize(
    ("name", "args", "needle"),
    [
        ("get_meal_plan", '{"days": 30}', "bad arguments"),
        ("get_meal_plan", '{"days": "many"}', "bad arguments"),
        ("get_meal_plan", '{"days": 2, "extra": 1}', "bad arguments"),
        ("get_energy_target", '{"x": 1}', "no arguments"),
        ("search_guidelines", "{}", "bad arguments"),
        ("search_guidelines", '{"question": "hi"}', "bad arguments"),
        ("get_meal_plan", "not json", "bad arguments"),
        ("get_meal_plan", "[1, 2]", "bad arguments"),
        ("delete_everything", "{}", "unknown tool"),
    ],
)
def test_untrusted_arguments_become_error_results_not_exceptions(
    name: str, args: str, needle: str
) -> None:
    r = tools().call(name, args)
    assert not r.ok and needle in r.error


def test_guideline_search_returns_cited_sentences_or_an_empty_insufficient_result() -> None:
    ev = FakeEvidence(SUPPORTED)
    r = tools(ev).call("search_guidelines", '{"question": "How much salt can I eat?"}')
    s = r.payload["sentences"][0]
    assert (s["doc_id"], s["page"], s["licence"]) == ("dga-2020-2025", "p. 13", "public domain")
    assert ev.questions == ["How much salt can I eat?"]
    none = tools().call("search_guidelines", '{"question": "best crypto to buy?"}')
    assert none.ok and none.payload == {"status": "insufficient_evidence", "sentences": []}
