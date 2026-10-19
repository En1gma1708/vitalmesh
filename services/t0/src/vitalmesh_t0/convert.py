"""Domain <-> wire conversion for recommendations (pure functions)."""

from __future__ import annotations

from datetime import datetime

from vitalmesh.events.v1 import common_pb2, payloads_pb2
from vitalmesh_wellness.claims import Claim, Provenance, Recommendation
from vitalmesh_wellness.guardian import Verdict

_PROVENANCE = {
    Provenance.COMPUTED: common_pb2.PROVENANCE_COMPUTED,
    Provenance.CITED: common_pb2.PROVENANCE_CITED,
    Provenance.INFERRED: common_pb2.PROVENANCE_INFERRED,
    Provenance.UNKNOWN: common_pb2.PROVENANCE_UNKNOWN,
}


def claim_to_proto(claim: Claim) -> payloads_pb2.RecommendationClaim:
    out = payloads_pb2.RecommendationClaim(
        text=claim.text, provenance=_PROVENANCE[claim.provenance]
    )
    if claim.source is not None:
        out.source = claim.source
    if claim.quantity is not None:
        out.metric = claim.quantity.metric
        out.quantity.value = claim.quantity.value
        out.quantity.unit = claim.quantity.unit
    return out


def to_released(
    rec: Recommendation, verdict: Verdict, engine_version: str, generated_at: datetime
) -> payloads_pb2.RecommendationReleased:
    if not verdict.cleared:
        raise ValueError("only a cleared recommendation can be released")
    out = payloads_pb2.RecommendationReleased(
        recommendation_id=rec.recommendation_id,
        engine_version=engine_version,
        rules_version=verdict.rules_version,
    )
    out.claims.extend(claim_to_proto(c) for c in rec.claims)
    out.generated_at.FromDatetime(generated_at.replace(tzinfo=None))
    return out


def to_blocked(rec: Recommendation, verdict: Verdict) -> payloads_pb2.RecommendationBlocked:
    out = payloads_pb2.RecommendationBlocked(
        recommendation_id=rec.recommendation_id, rules_version=verdict.rules_version
    )
    for v in verdict.violations:
        item = out.violations.add(rule=v.rule, message=v.message)
        if v.claim_text is not None:
            item.claim_text = v.claim_text
    return out
