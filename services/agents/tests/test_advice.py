from __future__ import annotations

from vitalmesh.events.v1 import common_pb2, payloads_pb2
from vitalmesh_agents.advice import (
    MAX_CLAIMS,
    advice_to_proto,
    citations_from_results,
    claim_numbers,
    claims_from_results,
    failed_advice,
)
from vitalmesh_common.run import Specialist
from vitalmesh_specialists import Advice, ToolResult

ENERGY = ToolResult(
    name="get_energy_target",
    ok=True,
    payload={
        "claims": [
            {
                "metric": "daily_energy_target",
                "value": 2124.0,
                "unit": "kcal",
                "text": "Aim for about 2124 kcal per day (maintain).",
            }
        ]
    },
)
PLAN = ToolResult(
    name="get_meal_plan",
    ok=True,
    payload={
        "days": [
            {
                "day": "2026-10-26",
                "meals": [
                    {"meal": "breakfast", "title": "Oat porridge", "portion": 1.0, "kcal": 400}
                ],
                "total_kcal": 1950,
            }
        ]
    },
)
SEARCH = ToolResult(
    name="search_guidelines",
    ok=True,
    payload={"status": "supported", "sentences": [{"text": "x"}]},
    meta={
        "citations": [
            {
                "doc_id": "pag-2018",
                "title": "PAG",
                "locator": "p. 34",
                "licence": "US federal government work",
                "source_url": "https://example.org/pag.pdf",
                "quote": "at least 150 minutes a week of moderate-intensity aerobic activity",
                "sentence": "Adults need at least 150 minutes of moderate activity a week.",
            }
        ]
    },
)
FAILED_TOOL = ToolResult(name="get_energy_target", ok=False, error="blocked by safety rules")


def advice(*results: ToolResult, status: str = "answered", text: str = "ok") -> Advice:
    return Advice(status=status, text=text, results=results, model_calls=2)


def test_computed_numbers_become_computed_claims_with_source_and_unit() -> None:
    (claim,) = claims_from_results([ENERGY])
    assert claim.provenance == common_pb2.PROVENANCE_COMPUTED
    assert claim.metric == "daily_energy_target"
    assert (claim.quantity.value, claim.quantity.unit) == (2124.0, "kcal")
    assert claim.source.startswith("vitalmesh_wellness.engine@")


def test_each_planned_day_is_a_computed_claim_so_its_kcal_is_a_claimed_number() -> None:
    (claim,) = claims_from_results([PLAN])
    assert claim.metric == "planned_day_energy" and claim.quantity.value == 1950
    assert "Oat porridge" in claim.text and claim.source.startswith("vitalmesh_wellness.mealplan")


def test_guideline_sentences_become_cited_claims_and_a_citation_with_the_verified_quote() -> None:
    claims = claims_from_results([SEARCH])
    assert [c.provenance for c in claims] == [common_pb2.PROVENANCE_CITED]
    assert claims[0].source == "pag-2018 p. 34"
    (cite,) = citations_from_results([SEARCH, SEARCH])  # duplicates are collapsed
    assert (cite.document, cite.locator) == ("pag-2018", "p. 34")
    assert cite.source_url.startswith("https://") and cite.licence
    assert "150 minutes" in cite.quote


def test_failed_tool_results_contribute_nothing() -> None:
    assert claims_from_results([FAILED_TOOL]) == []
    assert citations_from_results([FAILED_TOOL]) == []


def test_answered_with_tool_results_is_answered_and_carries_claims_and_counts() -> None:
    msg = advice_to_proto(Specialist.NUTRITION, advice(ENERGY, SEARCH))
    assert msg.status == payloads_pb2.ADVICE_ANSWERED
    assert msg.specialist == common_pb2.SPECIALIST_NUTRITION
    assert len(msg.claims) == 2 and len(msg.citations) == 1
    assert (msg.model_calls, msg.tool_calls) == (2, 2)


def test_fallback_status_is_kept() -> None:
    msg = advice_to_proto(Specialist.MOVEMENT_RECOVERY, advice(ENERGY, status="fallback"))
    assert msg.status == payloads_pb2.ADVICE_FALLBACK


def test_an_answer_that_used_no_tool_result_is_declined_and_grounds_nothing() -> None:
    msg = advice_to_proto(
        Specialist.CONDITION_MEDICATION,
        advice(FAILED_TOOL, text="I cannot advise on medication; please ask a doctor."),
    )
    assert msg.status == payloads_pb2.ADVICE_DECLINED
    assert len(msg.claims) == 0 and len(msg.citations) == 0
    assert "doctor" in msg.text


def test_failed_advice_carries_the_code_and_no_content() -> None:
    msg = failed_advice(Specialist.MOVEMENT_RECOVERY, "llm.unavailable")
    assert msg.status == payloads_pb2.ADVICE_FAILED
    assert msg.failure_code == "llm.unavailable" and msg.text == ""


def test_claims_are_capped() -> None:
    many = ToolResult(
        name="get_activity_summary",
        ok=True,
        payload={
            "claims": [
                {"metric": f"m{i}", "value": float(i), "unit": "min", "text": f"claim {i}"}
                for i in range(MAX_CLAIMS + 10)
            ]
        },
    )
    assert len(claims_from_results([many])) == MAX_CLAIMS


def test_claim_numbers_cover_text_and_quantity_only() -> None:
    claims = claims_from_results([ENERGY, PLAN])
    assert {2124.0, 1950.0, 2026.0, 10.0, 26.0} <= claim_numbers(claims)
    assert 9999.0 not in claim_numbers(claims)
