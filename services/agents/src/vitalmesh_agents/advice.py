"""From a specialist's checked ``Advice`` to the typed ``AdviceCompleted`` event (D68). Pure.

Everything numeric or cited in an event is a CLAIM with provenance, built from the tool results
(code), never parsed out of the model's prose:

* a computed number the tools returned -> ``PROVENANCE_COMPUTED`` with its calculator and quantity;
* a guideline sentence the Evidence agent verified -> ``PROVENANCE_CITED`` with the document and
  page, plus a ``Citation`` carrying the exact quote, licence and URL.

An answer that used no tool result at all is ``DECLINED``: it grounds nothing, so downstream
stages may show its words (a refusal or a clinician pointer) but never count it as evidence.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from vitalmesh.events.v1 import common_pb2, payloads_pb2
from vitalmesh_common.run import Specialist, specialist_to_proto
from vitalmesh_specialists import Advice, ToolResult
from vitalmesh_wellness.engine import ENGINE_VERSION

DEFAULT_SOURCE = f"vitalmesh_wellness.engine@{ENGINE_VERSION}"
PLAN_SOURCE = "vitalmesh_wellness.mealplan@1"
MAX_CLAIMS = 24  # an event stays small; the cap is reported, never silently exceeded
MAX_CITATIONS = 8


def _computed(
    metric: str, value: float, unit: str, text: str, source: str
) -> payloads_pb2.RecommendationClaim:
    claim = payloads_pb2.RecommendationClaim(
        text=text, provenance=common_pb2.PROVENANCE_COMPUTED, source=source, metric=metric
    )
    claim.quantity.value = float(value)
    claim.quantity.unit = unit
    return claim


def claims_from_results(results: Sequence[ToolResult]) -> list[payloads_pb2.RecommendationClaim]:
    out: list[payloads_pb2.RecommendationClaim] = []
    for r in results:
        if not r.ok:
            continue
        for c in r.payload.get("claims", []):
            out.append(
                _computed(
                    c["metric"],
                    c["value"],
                    c["unit"],
                    c["text"],
                    c.get("source") or DEFAULT_SOURCE,
                )
            )
        if r.name == "get_meal_plan":
            for day in r.payload.get("days", []):
                meals = ", ".join(f"{m['meal']}: {m['title']}" for m in day["meals"])
                text = f"{day['day']}: {meals} (about {day['total_kcal']} kcal)."
                out.append(
                    _computed("planned_day_energy", day["total_kcal"], "kcal", text, PLAN_SOURCE)
                )
        for cite in r.meta.get("citations", []):
            out.append(
                payloads_pb2.RecommendationClaim(
                    text=cite["sentence"],
                    provenance=common_pb2.PROVENANCE_CITED,
                    source=f"{cite['doc_id']} {cite['locator']}",
                )
            )
    return out[:MAX_CLAIMS]


def citations_from_results(results: Sequence[ToolResult]) -> list[payloads_pb2.Citation]:
    seen: set[tuple[str, str, str]] = set()
    out: list[payloads_pb2.Citation] = []
    for r in results:
        if not r.ok:
            continue
        for cite in r.meta.get("citations", []):
            key = (cite["doc_id"], cite["locator"], cite["quote"])
            if key in seen:
                continue
            seen.add(key)
            out.append(
                payloads_pb2.Citation(
                    document=cite["doc_id"],
                    locator=cite["locator"],
                    licence=cite["licence"],
                    source_url=cite["source_url"],
                    quote=cite["quote"],
                )
            )
    return out[:MAX_CITATIONS]


def advice_to_proto(kind: Specialist, advice: Advice) -> payloads_pb2.AdviceCompleted:
    grounded = any(r.ok for r in advice.results)
    if not grounded:
        status = payloads_pb2.ADVICE_DECLINED
    elif advice.status == "answered":
        status = payloads_pb2.ADVICE_ANSWERED
    else:
        status = payloads_pb2.ADVICE_FALLBACK
    msg = payloads_pb2.AdviceCompleted(
        specialist=specialist_to_proto(kind),
        status=status,
        text=advice.text,
        model_calls=advice.model_calls,
        tool_calls=len(advice.results),
    )
    if grounded:
        msg.claims.extend(claims_from_results(advice.results))
        msg.citations.extend(citations_from_results(advice.results))
    return msg


def failed_advice(kind: Specialist, code: str) -> payloads_pb2.AdviceCompleted:
    """Published instead of staying silent, so the Aggregator does not wait for the deadline on
    a failure it could have been told about."""
    return payloads_pb2.AdviceCompleted(
        specialist=specialist_to_proto(kind),
        status=payloads_pb2.ADVICE_FAILED,
        failure_code=code,
    )


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def claim_numbers(claims: Sequence[Any]) -> set[float]:
    """Every number a set of claims makes available to prose: in the text or the quantity."""
    out: set[float] = set()
    for c in claims:
        for m in _NUMBER.findall(c.text):
            try:
                out.add(float(m.replace(",", "")))
            except ValueError:
                continue
        if c.HasField("quantity"):
            out.add(float(c.quantity.value))
    return out
