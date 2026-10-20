"""API gateway (FastAPI).

Two jobs:

* ``POST /v1/measurements``: the ingress safety boundary. Validates the metric against a
  registry (known metric, registered unit, plausible range, no future timestamps) and only
  then produces ``measurement.recorded`` to ``user.events``. Answers 202 once the broker has
  acknowledged the write.
* ``GET /v1/users/{user_id}/recommendation``: the synchronous in-process T0 path (profile ->
  engine -> Guardian, no Kafka hop). Only a Guardian-cleared recommendation is returned; the
  response is health-only (D40): plain-language statements, no tiers, agents or rule ids.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_common.envelope import EventMeta, utcnow
from vitalmesh_common.failure import EnvelopeError
from vitalmesh_common.processor import EventSerde, Publisher
from vitalmesh_common.topics import USER_EVENTS
from vitalmesh_processor.labstore import LabStore
from vitalmesh_t0.decision import decide
from vitalmesh_t0.profiles import ProfileStore
from vitalmesh_wellness.claims import Provenance
from vitalmesh_wellness.guardian import Guardian
from vitalmesh_wellness.labs import LabError, interpret

USER_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_FUTURE = timedelta(minutes=5)
_NAMESPACE = uuid.UUID("0c4e6d1a-3b2f-4f7a-9d58-2a1b7c9e4f10")


class MetricSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    unit: str
    low: float
    high: float


# The ingress registry: only these metrics, in these units, within plausible human ranges.
# A value outside the range is rejected here, never clamped (an implausible number must not
# become state that rules act on).
METRICS: dict[str, MetricSpec] = {
    "body_weight": MetricSpec(unit="kg", low=20, high=400),
    "resting_heart_rate": MetricSpec(unit="bpm", low=25, high=220),
    "sleep_duration": MetricSpec(unit="h", low=0, high=24),
    "step_count": MetricSpec(unit="count", low=0, high=200_000),
    "active_minutes": MetricSpec(unit="min", low=0, high=1440),
    "energy_intake": MetricSpec(unit="kcal", low=0, high=20_000),
}


class MeasurementIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    household_id: str = "default"
    metric_type: str
    value: float
    unit: str
    measured_at: datetime
    client_event_id: Annotated[str | None, Field(max_length=128)] = None  # idempotency

    @field_validator("user_id", "household_id")
    @classmethod
    def _ids(cls, v: str) -> str:
        if not USER_ID.match(v):
            raise ValueError("ids are 1-64 characters of letters, digits, '.', '_' or '-'")
        return v


class LabIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    household_id: str = "default"
    marker_name: Annotated[str, Field(min_length=1, max_length=120)]
    value: float | None = None
    unit: Annotated[str | None, Field(max_length=32)] = None
    text: Annotated[str | None, Field(max_length=200)] = None
    reference_range: Annotated[str, Field(max_length=200)] = ""
    collected_at: datetime
    derived: bool = False
    client_event_id: Annotated[str | None, Field(max_length=128)] = None

    @field_validator("user_id", "household_id")
    @classmethod
    def _ids(cls, v: str) -> str:
        if not USER_ID.match(v):
            raise ValueError("ids are 1-64 characters of letters, digits, '.', '_' or '-'")
        return v


class LabOut(BaseModel):
    name: str
    value: float | None
    unit: str | None
    text: str | None
    reference_range: str
    status: str  # low / normal / high / not classified
    collected_at: datetime
    calculated: bool


class Accepted(BaseModel):
    event_id: str
    correlation_id: str


class Item(BaseModel):
    text: str
    value: float | None
    unit: str | None
    basis: str


class RecommendationOut(BaseModel):
    recommendation_id: str
    items: list[Item]


_BASIS = {
    Provenance.COMPUTED: "Calculated from your profile and recent data",
    Provenance.CITED: "Based on published guidance",
    Provenance.INFERRED: "A reasoned estimate from your data",
    Provenance.UNKNOWN: "Not verified",
}


def validate_measurement(m: MeasurementIn, now: datetime) -> None:
    spec = METRICS.get(m.metric_type)
    if spec is None:
        raise ValueError(f"unknown metric {m.metric_type!r}")
    if m.unit != spec.unit:
        raise ValueError(f"{m.metric_type} must be sent in {spec.unit!r}, got {m.unit!r}")
    if not math.isfinite(m.value) or not spec.low <= m.value <= spec.high:
        raise ValueError(f"{m.metric_type} must be between {spec.low} and {spec.high} {spec.unit}")
    if m.measured_at.tzinfo is None:
        raise ValueError("measured_at must include a timezone")
    if m.measured_at > now + MAX_FUTURE:
        raise ValueError("measured_at is in the future")


def create_app(
    profiles: ProfileStore,
    serde: EventSerde,
    publisher: Publisher,
    guardian: Guardian | None = None,
    clock: Callable[[], datetime] = utcnow,
    labs: LabStore | None = None,
) -> FastAPI:
    app = FastAPI(title="VitalMesh API", version="0.1.0")
    gate = guardian or Guardian()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/measurements", status_code=202, response_model=Accepted)
    def post_measurement(m: MeasurementIn) -> Accepted:
        try:
            validate_measurement(m, clock())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        correlation_id = str(uuid.uuid4())
        event_id = (
            str(uuid.uuid5(_NAMESPACE, f"{m.user_id}:{m.client_event_id}"))
            if m.client_event_id
            else str(uuid.uuid4())
        )
        meta = EventMeta(
            event_id=event_id,
            correlation_id=correlation_id,
            user_id=m.user_id,
            household_id=m.household_id,
            source="api" if m.client_event_id else None,
            external_id=m.client_event_id,
            type="measurement.recorded",
            occurred_at=clock(),
            trace_id=correlation_id,
        )
        payload = payloads_pb2.MeasurementRecorded(metric_type=m.metric_type)
        payload.value.value, payload.value.unit = m.value, m.unit
        payload.measured_at.FromDatetime(m.measured_at.astimezone(UTC).replace(tzinfo=None))
        try:
            data = serde.serialize_event(USER_EVENTS, meta, payload)
            publisher.publish(USER_EVENTS, m.user_id, data)
            publisher.flush()
        except EnvelopeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # broker or registry down: tell the client to retry
            raise HTTPException(status_code=503, detail="temporarily unavailable") from exc
        return Accepted(event_id=event_id, correlation_id=correlation_id)

    @app.post("/v1/labs", status_code=202, response_model=Accepted)
    def post_lab(m: LabIn) -> Accepted:
        now = clock()
        try:
            if m.collected_at.tzinfo is not None and m.collected_at > now + MAX_FUTURE:
                raise LabError("collected_at is in the future")
            result = interpret(
                m.marker_name,
                m.collected_at,
                value=m.value,
                unit=m.unit,
                text=m.text,
                reference_text=m.reference_range,
                derived=m.derived,
            )
        except LabError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        correlation_id = str(uuid.uuid4())
        event_id = (
            str(uuid.uuid5(_NAMESPACE, f"lab:{m.user_id}:{m.client_event_id}"))
            if m.client_event_id
            else str(uuid.uuid4())
        )
        meta = EventMeta(
            event_id=event_id,
            correlation_id=correlation_id,
            user_id=m.user_id,
            household_id=m.household_id,
            source="api" if m.client_event_id else None,
            external_id=m.client_event_id,
            type="lab.result.recorded",
            occurred_at=now,
            trace_id=correlation_id,
        )
        payload = payloads_pb2.LabResultRecorded(
            marker_code=result.loinc or "", marker_name=result.name, derived=result.derived
        )
        if result.text is not None:
            payload.text = result.text
        else:
            assert result.value is not None and result.unit is not None
            payload.numeric.value, payload.numeric.unit = result.value, result.unit
        payload.reference.raw_text = result.reference.raw_text
        payload.reference.parsed = result.reference.parsed
        if result.reference.low is not None:
            payload.reference.low = result.reference.low
        if result.reference.high is not None:
            payload.reference.high = result.reference.high
        payload.collected_at.FromDatetime(result.collected_at.astimezone(UTC).replace(tzinfo=None))
        try:
            publisher.publish(
                USER_EVENTS, m.user_id, serde.serialize_event(USER_EVENTS, meta, payload)
            )
            publisher.flush()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="temporarily unavailable") from exc
        return Accepted(event_id=event_id, correlation_id=correlation_id)

    @app.get("/v1/users/{user_id}/labs", response_model=list[LabOut])
    def get_labs(user_id: str) -> list[LabOut]:
        if not USER_ID.match(user_id):
            raise HTTPException(status_code=422, detail="invalid user id")
        if labs is None:
            raise HTTPException(status_code=503, detail="temporarily unavailable")
        return [
            LabOut(
                name=r.name,
                value=r.value,
                unit=r.unit,
                text=r.text,
                reference_range=r.reference.raw_text,
                status="not classified" if r.flag.value == "unclassified" else r.flag.value,
                collected_at=r.collected_at,
                calculated=r.derived,
            )
            for r in labs.list(user_id)
        ]

    @app.get("/v1/users/{user_id}/recommendation", response_model=RecommendationOut)
    def get_recommendation(user_id: str) -> RecommendationOut:
        if not USER_ID.match(user_id):
            raise HTTPException(status_code=422, detail="invalid user id")
        record = profiles.get(user_id)
        if record is None:
            raise HTTPException(status_code=404, detail="no profile for this user")
        try:
            decision = decide(record, str(uuid.uuid4()), gate)
        except EnvelopeError as exc:
            raise HTTPException(status_code=422, detail="profile data is not usable") from exc
        if not decision.verdict.cleared:
            # Nothing unsafe leaves the system, and nothing internal is revealed (D40).
            raise HTTPException(
                status_code=409, detail="no recommendation is available for you right now"
            )
        items: list[Item] = []
        for claim in decision.recommendation.claims:
            q: Any = claim.quantity
            items.append(
                Item(
                    text=claim.text,
                    value=q.value if q else None,
                    unit=q.unit if q else None,
                    basis=_BASIS[claim.provenance],
                )
            )
        return RecommendationOut(
            recommendation_id=decision.recommendation.recommendation_id, items=items
        )

    return app
