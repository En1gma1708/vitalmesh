"""API gateway (FastAPI).

Two jobs:

* ``POST /v1/measurements``: the ingress safety boundary. Validates the metric against a
  registry (known metric, registered unit, plausible range, no future timestamps) and only
  then produces ``measurement.recorded`` to ``user.events``. Answers 202 once the broker has
  acknowledged the write.
* ``POST /v1/users/{user_id}/questions``: a free-text question. The ingress screen runs first
  (a declined request never reaches Kafka); otherwise ``question.asked`` starts a T3 run and the
  caller gets 202 with the correlation id. The answer is not returned here: it is released later
  on ``recommendations.released`` (or held for human review).
* ``GET /v1/users/{user_id}/recommendation``: the synchronous in-process T0 path (profile ->
  engine -> Guardian, no Kafka hop). Only a Guardian-cleared recommendation is returned; the
  response is health-only (D40): plain-language statements, no tiers, agents or rule ids.
"""

from __future__ import annotations

import math
import re
import secrets
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from html import escape
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool
from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_accounts import KEEP, QaHistory, ReviewQueue
from vitalmesh_common.envelope import EventMeta, utcnow
from vitalmesh_common.failure import EnvelopeError
from vitalmesh_common.lag import LagRow
from vitalmesh_common.processor import EventSerde, Publisher
from vitalmesh_common.topics import QUESTIONS_ASKED, USER_EVENTS
from vitalmesh_concierge.screen import REFUSALS, screen_question
from vitalmesh_processor.history import HistoryStore
from vitalmesh_processor.labstore import LabStore
from vitalmesh_stores import (
    InMemoryNudgeStore,
    InMemoryPendingImports,
    NudgeStore,
    PendingImports,
    Prefs,
    RunLog,
)
from vitalmesh_t0.decision import decide
from vitalmesh_t0.profiles import ProfileStore
from vitalmesh_wellness.calculators import OutOfRange
from vitalmesh_wellness.claims import Provenance
from vitalmesh_wellness.engine import build_meal_plan_recommendation
from vitalmesh_wellness.guardian import Guardian
from vitalmesh_wellness.labs import LabError, LabResult, interpret
from vitalmesh_wellness.mealplan import PlanError
from vitalmesh_wellness.recipes import BY_ID
from vitalmesh_wellness.trends import WEEKLY_ACTIVE_MINUTES_GOAL, trailing_average, weekly_totals

from vitalmesh_api.clinician import install_clinician
from vitalmesh_api.googlecal import GoogleClient, TokenBox
from vitalmesh_api.healthimport import (
    MAX_UPLOAD_BYTES,
    ImportRefused,
    parse_upload,
    summarise,
)
from vitalmesh_api.integrations import install_integrations
from vitalmesh_api.labextract import MAX_BYTES as LAB_MAX_BYTES
from vitalmesh_api.labextract import ExtractRefused, OcrBusy
from vitalmesh_api.labextract import extract as extract_labs
from vitalmesh_api.opsview import (
    EvalRow,
    GraphOut,
    LagGroup,
    Percentiles,
    RunSummary,
    StageTiming,
    percentiles,
    redact,
    stage_timings,
    summarise_run,
)
from vitalmesh_api.runview import Answer, RunView, answer_of, view_of
from vitalmesh_api.security import Auth, install, require_self

STATIC = Path(__file__).parent / "static"
IMPORT_ID = re.compile(r"^[A-Za-z0-9_-]{8,32}$")
NOTIFICATION_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
MAX_IMPORT_EVENTS = 2000
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


class LabPick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    i: Annotated[int, Field(ge=0, le=200)]
    value: float
    unit: Annotated[str, Field(min_length=1, max_length=32)]


class LabUploadConfirm(BaseModel):
    """Only the rows the person ticked, with the value and unit as they left them."""

    model_config = ConfigDict(extra="forbid")

    collected_on: date
    rows: Annotated[list[LabPick], Field(min_length=1, max_length=60)]


class NudgePrefsIn(BaseModel):
    """The switch for nudges and the browser's timezone, so quiet hours are the person's own."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    timezone: Annotated[str, Field(min_length=1, max_length=64)] = "UTC"

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError("unknown timezone") from None
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


class QuestionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Annotated[str, Field(min_length=1, max_length=500)]
    session_id: Annotated[str, Field(max_length=64)] = ""


class QuestionOut(BaseModel):
    status: str  # "accepted" (a run started) or "declined" (screened out, nothing was started)
    correlation_id: str | None = None
    message: str | None = None


class QaItem(BaseModel):
    correlation_id: str
    question: str
    status: str  # asked | released | blocked | needs_review
    asked_at: datetime
    answer: Answer | None


class OpsRunsOut(BaseModel):
    runs: list[RunSummary]
    latency: Percentiles  # released runs, question asked -> answer released
    latency_by_tier: dict[str, Percentiles]
    stages_by_tier: dict[str, list[StageTiming]]  # where the time goes, per step, per tier


class OpsRunOut(BaseModel):
    summary: RunSummary
    entries: list[dict[str, Any]]  # redacted: no question or answer text


class OpsTiersOut(BaseModel):
    tiers: dict[str, int]
    cohort: dict[str, int]
    planned: int
    cache_hit_share: float | None  # T1 runs / all planned runs
    no_model_share: float | None  # (T0 + T1) / all planned runs


class OpsNudgesOut(BaseModel):
    """Engagement counters (D87): counts only, never a person, a message or a model's reason."""

    counters: dict[str, int]
    decisions_by_model: int  # model decisions compared with the plain rule (cached copies excluded)
    model_agreement: float | None  # share of model decisions equal to the plain rule's
    cache_hit_share: float | None  # cached decisions / (cached + model-or-fallback) decisions
    vetoed_by_delivery: int  # nudges Delivery refused although the agent asked


class Accepted(BaseModel):
    event_id: str
    correlation_id: str


class Item(BaseModel):
    text: str
    value: float | None
    unit: str | None
    basis: str


class MealOut(BaseModel):
    meal: str
    title: str
    portion: float
    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float


class DayOut(BaseModel):
    day: date
    kcal: float
    meals: list[MealOut]


class MealPlanOut(BaseModel):
    average_daily_kcal: float
    days: list[DayOut]


class SeriesPoint(BaseModel):
    t: datetime
    v: float


class WeekTotal(BaseModel):
    week_start: date
    total: float


class SeriesOut(BaseModel):
    metric: str
    unit: str
    points: list[SeriesPoint]
    average: list[SeriesPoint]  # 7-day trailing average (smooth metrics only)
    weekly: list[WeekTotal]  # weekly totals (activity metrics only)
    target: float | None  # a daily target, when the user has one
    weekly_goal: float | None


SMOOTHED = {"body_weight", "resting_heart_rate"}
WEEKLY = {"active_minutes", "step_count"}


class RecommendationOut(BaseModel):
    recommendation_id: str
    items: list[Item]


_BASIS = {
    Provenance.COMPUTED: "Calculated from your profile and recent data",
    Provenance.CITED: "Based on published guidance",
    Provenance.INFERRED: "A reasoned estimate from your data",
    Provenance.UNKNOWN: "Not verified",
}


def sse(event: str, data: str) -> str:
    """One server-sent event frame."""
    return f"event: {event}\ndata: {data}\n\n"


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
    history: HistoryStore | None = None,
    ops: Callable[[], dict[str, dict[str, int]]] | None = None,
    runs: RunLog | None = None,
    stream_poll_s: float = 0.3,
    stream_max_s: float = 300.0,
    stream_appear_s: float = 5.0,
    auth: Auth | None = None,
    imports: PendingImports | None = None,
    nudges: NudgeStore | None = None,
    google: GoogleClient | None = None,
    token_box: TokenBox | None = None,
    qa: QaHistory | None = None,
    lag: Callable[[], list[LagRow]] | None = None,
    graph: Callable[[], GraphOut | None] | None = None,
    reviews: ReviewQueue | None = None,
    evals: Callable[[], list[EvalRow]] | None = None,
    notice: str = "",
) -> FastAPI:
    app = FastAPI(title="VitalMesh API", version="0.1.0")
    gate = guardian or Guardian()
    if auth is not None:
        install(app, auth)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/measurements", status_code=202, response_model=Accepted)
    def post_measurement(m: MeasurementIn, request: Request) -> Accepted:
        require_self(request, m.user_id)
        try:
            validate_measurement(m, clock())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return emit_measurement(m)

    def emit_measurement(m: MeasurementIn, flush: bool = True) -> Accepted:
        """Publish one validated measurement (shared by the form and the file import)."""
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
            if flush:
                publisher.flush()
        except EnvelopeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # broker or registry down: tell the client to retry
            raise HTTPException(status_code=503, detail="temporarily unavailable") from exc
        return Accepted(event_id=event_id, correlation_id=correlation_id)

    def emit_lab(
        user_id: str,
        household_id: str,
        result: LabResult,
        client_event_id: str | None,
        now: datetime,
    ) -> Accepted:
        """Publish one validated lab result. Shared by the hand-typed form and the report upload."""
        correlation_id = str(uuid.uuid4())
        event_id = (
            str(uuid.uuid5(_NAMESPACE, f"lab:{user_id}:{client_event_id}"))
            if client_event_id
            else str(uuid.uuid4())
        )
        meta = EventMeta(
            event_id=event_id,
            correlation_id=correlation_id,
            user_id=user_id,
            household_id=household_id,
            source="api" if client_event_id else None,
            external_id=client_event_id,
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
                USER_EVENTS, user_id, serde.serialize_event(USER_EVENTS, meta, payload)
            )
            publisher.flush()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="temporarily unavailable") from exc
        return Accepted(event_id=event_id, correlation_id=correlation_id)

    if auth is not None:
        pending: PendingImports = imports if imports is not None else InMemoryPendingImports()

        def own_id(request: Request) -> str:
            return str(request.state.claims.user_id)

        install_integrations(app, auth, google, token_box or TokenBox(), pending, clock)

        @app.post("/v1/me/imports", status_code=201)
        async def start_import(request: Request) -> dict[str, Any]:
            """Read an uploaded health export and show what it holds. Stores nothing as data."""
            user_id = own_id(request)
            if not auth.limiter.hit(f"import:{user_id}", 5, 3600):
                raise HTTPException(status_code=429, detail="too many uploads; try again later")
            try:
                declared = int(request.headers.get("content-length", "0"))
            except ValueError:
                declared = 0
            if declared > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="that file is bigger than 50 MB")
            body = bytearray()
            async for chunk in request.stream():
                body += chunk
                if len(body) > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="that file is bigger than 50 MB")
            now = clock()
            try:
                parsed = await run_in_threadpool(
                    parse_upload, bytes(body), request.headers.get("x-filename", "")[:200], now
                )
            except ImportRefused as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            usable, rejected = [], 0
            for r in parsed.readings:
                candidate = MeasurementIn(
                    user_id=user_id, metric_type=r.metric, value=r.value, unit=r.unit,
                    measured_at=r.measured_at,
                )  # fmt: skip
                try:
                    validate_measurement(candidate, now)
                    usable.append(r)
                except ValueError:
                    rejected += 1  # implausible: never clamped, never stored
            if not usable:
                raise HTTPException(
                    status_code=422, detail="I found no usable readings in that file and period."
                )
            if len(usable) > MAX_IMPORT_EVENTS:
                raise HTTPException(status_code=422, detail="too many readings; send fewer days")
            import_id = secrets.token_urlsafe(12)
            pending.put(
                user_id,
                import_id,
                {
                    "source": parsed.source,
                    "readings": [
                        {"metric": r.metric, "day": r.day.isoformat(), "value": r.value,
                         "unit": r.unit, "at": r.measured_at.isoformat()}
                        for r in usable
                    ],
                },
            )  # fmt: skip
            return {
                "import_id": import_id,
                "source": parsed.source,
                "total": len(usable),
                "summary": summarise(usable),
                "rejected": rejected,
                "skipped": parsed.skipped,
            }

        @app.post("/v1/me/imports/{import_id}/confirm", status_code=202)
        def confirm_import(import_id: str, request: Request) -> dict[str, int]:
            user_id = own_id(request)
            item = pending.get(user_id, import_id) if IMPORT_ID.match(import_id) else None
            if item is None or "readings" not in item:  # a lab upload has no readings
                raise HTTPException(status_code=404, detail="that import expired; upload again")
            sent = 0
            for r in item["readings"]:
                m = MeasurementIn(
                    user_id=user_id, metric_type=r["metric"], value=r["value"], unit=r["unit"],
                    measured_at=datetime.fromisoformat(r["at"]),
                    # the same day and value always give the same id, so importing a file twice
                    # does not double anything; a corrected value is a new reading
                    client_event_id=f"imp:{r['metric']}:{r['day']}:{r['value']}",
                )  # fmt: skip
                validate_measurement(m, clock())
                emit_measurement(m, flush=False)
                sent += 1
            try:
                publisher.flush()
            except Exception as exc:
                raise HTTPException(status_code=503, detail="temporarily unavailable") from exc
            pending.delete(user_id, import_id)
            return {"imported": sent}

        @app.delete("/v1/me/imports/{import_id}")
        def cancel_import(import_id: str, request: Request) -> dict[str, str]:
            if IMPORT_ID.match(import_id):
                pending.delete(own_id(request), import_id)
            return {"status": "cancelled"}

        # ---- gentle reminders (D87): off until the person turns them on; the agents decide, the
        # ---- person's own switch and dismissals are all they can change here
        nudge_store: NudgeStore = nudges if nudges is not None else InMemoryNudgeStore()

        @app.get("/v1/me/nudges")
        def get_nudge_prefs(request: Request) -> dict[str, Any]:
            prefs = nudge_store.get_prefs(own_id(request))
            return {"enabled": prefs.enabled, "timezone": prefs.timezone}

        @app.put("/v1/me/nudges")
        def put_nudge_prefs(body: NudgePrefsIn, request: Request) -> dict[str, Any]:
            user_id = own_id(request)
            nudge_store.set_prefs(user_id, Prefs(body.enabled, body.timezone))
            auth.audit(request, user_id, "nudges_changed", enabled=body.enabled)
            return {"enabled": body.enabled, "timezone": body.timezone}

        @app.get("/v1/me/notifications")
        def get_notifications(request: Request) -> list[dict[str, Any]]:
            return [
                {"id": i.id, "kind": i.kind, "text": i.text, "at": i.at.isoformat()}
                for i in nudge_store.items(own_id(request))
                if not i.dismissed
            ][:20]

        @app.post("/v1/me/notifications/{notification_id}/dismiss")
        def dismiss_notification(notification_id: str, request: Request) -> dict[str, str]:
            if not NOTIFICATION_ID.match(notification_id) or not nudge_store.dismiss(
                own_id(request), notification_id
            ):
                raise HTTPException(status_code=404, detail="no such notification")
            return {"status": "dismissed"}

        # ---- lab report upload (D86): read -> person checks every row -> save on confirmation
        @app.post("/v1/me/lab-uploads", status_code=201)
        async def start_lab_upload(request: Request) -> dict[str, Any]:
            user_id = own_id(request)
            if not auth.limiter.hit(f"labup:{user_id}", 6, 3600):
                raise HTTPException(status_code=429, detail="too many uploads; try again later")
            body = bytearray()
            async for chunk in request.stream():
                body += chunk
                if len(body) > LAB_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="that file is bigger than 15 MB")
            try:
                found = await run_in_threadpool(extract_labs, bytes(body))
            except OcrBusy as exc:
                raise HTTPException(status_code=429, detail=str(exc)) from exc
            except ExtractRefused as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            rows = [
                {"name": c.name, "marker": c.marker, "value": c.value, "unit": c.unit,
                 "reference": c.reference, "line": c.line, "problem": c.problem}
                for c in found.candidates
            ]  # fmt: skip
            upload_id = secrets.token_urlsafe(12)
            pending.put(user_id, upload_id, {"kind": "labs", "rows": rows})
            return {
                "upload_id": upload_id,
                "method": found.method,
                "collected_on": found.collected_on.isoformat() if found.collected_on else None,
                "rows": rows,
                "unmatched_lines": found.unmatched_lines,
            }

        @app.post("/v1/me/lab-uploads/{upload_id}/confirm", status_code=202)
        def confirm_lab_upload(
            upload_id: str, body: LabUploadConfirm, request: Request
        ) -> dict[str, int]:
            user_id = own_id(request)
            item = pending.get(user_id, upload_id) if IMPORT_ID.match(upload_id) else None
            if item is None or item.get("kind") != "labs":
                raise HTTPException(status_code=404, detail="that upload expired; upload again")
            now = clock()
            at = min(
                datetime.combine(body.collected_on, dtime(12, 0), tzinfo=UTC),
                now - timedelta(minutes=1),
            )
            if body.collected_on > now.date():
                raise HTTPException(status_code=422, detail="the collection date is in the future")
            results: list[tuple[LabResult, str]] = []
            for pick in body.rows:
                if not 0 <= pick.i < len(item["rows"]):
                    raise HTTPException(status_code=422, detail="unknown row")
                src = item["rows"][pick.i]
                try:  # the same rules as a hand-typed result
                    result = interpret(
                        src["name"], at, value=pick.value, unit=pick.unit,
                        reference_text=src["reference"],
                    )  # fmt: skip
                except LabError as exc:
                    raise HTTPException(status_code=422, detail=f"{src['name']}: {exc}") from exc
                results.append((result, f"labup:{src['marker']}:{body.collected_on}:{pick.value}"))
            for result, event_key in results:
                emit_lab(user_id, "default", result, event_key, now)
            pending.delete(user_id, upload_id)
            return {"saved": len(results)}

    @app.post("/v1/labs", status_code=202, response_model=Accepted)
    def post_lab(m: LabIn, request: Request) -> Accepted:
        require_self(request, m.user_id)
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

        return emit_lab(m.user_id, m.household_id, result, m.client_event_id, now)

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

    @app.get("/v1/users/{user_id}/series/{metric}", response_model=SeriesOut)
    def get_series(user_id: str, metric: str, days: int = 90) -> SeriesOut:
        if not USER_ID.match(user_id):
            raise HTTPException(status_code=422, detail="invalid user id")
        spec = METRICS.get(metric)
        if spec is None:
            raise HTTPException(status_code=404, detail="unknown metric")
        if not 1 <= days <= 365:
            raise HTTPException(status_code=422, detail="days must be between 1 and 365")
        if history is None:
            raise HTTPException(status_code=503, detail="temporarily unavailable")
        points = history.series(user_id, metric, since=clock() - timedelta(days=days))
        pairs = [(p.t, p.value) for p in points]
        target: float | None = None
        record = profiles.get(user_id)
        if record is not None and metric in {"sleep_duration", "energy_intake"}:
            try:
                rec = decide(record, "chart", gate).recommendation
            except EnvelopeError:
                rec = None
            wanted = {"sleep_duration": "sleep_target", "energy_intake": "daily_energy_target"}
            for c in rec.claims if rec else ():
                if c.quantity and c.quantity.metric == wanted[metric]:
                    target = c.quantity.value
        return SeriesOut(
            metric=metric,
            unit=spec.unit,
            points=[SeriesPoint(t=t, v=v) for t, v in pairs],
            average=[SeriesPoint(t=t, v=v) for t, v in trailing_average(pairs)]
            if metric in SMOOTHED
            else [],
            weekly=[WeekTotal(week_start=w, total=v) for w, v in weekly_totals(pairs)]
            if metric in WEEKLY
            else [],
            target=target,
            weekly_goal=WEEKLY_ACTIVE_MINUTES_GOAL if metric == "active_minutes" else None,
        )

    def page(request: Request, name: str) -> HTMLResponse:
        """A static page with this response's script and style nonce filled in (the security
        policy allows only code that carries it)."""
        nonce = getattr(request.state, "nonce", "")
        html = (STATIC / name).read_text(encoding="utf-8")
        html = html.replace("<script>", f'<script nonce="{nonce}">')
        html = html.replace("<style>", f'<style nonce="{nonce}">')
        if "/*@vm.css*/" in html:  # the shared design system (D106), inside the nonce'd style block
            html = html.replace("/*@vm.css*/", (STATIC / "vm.css").read_text(encoding="utf-8"))
        if notice:  # an operator-set banner (for example "made-up data only"), escaped, every page
            banner = (
                '<div role="note" style="background:var(--accent-soft,#e8f1ee);'
                "color:var(--fg,#1b1f24);padding:10px 16px;text-align:center;font-size:14px;"
                f'border-bottom:1px solid var(--grid,#c9d3d0)">{escape(notice)}</div>'
            )
            html = html.replace("<body>", "<body>" + banner, 1)
        return HTMLResponse(html)

    if reviews is not None:
        install_clinician(app, reviews, serde, publisher, clock, auth)

    @app.get("/clinician", response_class=HTMLResponse)
    def clinician_page(request: Request) -> HTMLResponse:
        # public HTML; the queue and every decision go through /v1/clinician/, clinicians only
        return page(request, "clinician.html")

    @app.get("/ops", response_class=HTMLResponse)
    def ops_page(request: Request) -> HTMLResponse:
        # the page is public HTML; every number on it comes from /v1/ops/*, which needs an operator
        return page(request, "ops.html")

    if auth is not None:

        @app.get("/", response_class=HTMLResponse)
        def welcome(request: Request) -> HTMLResponse:
            return page(request, "welcome.html")

        @app.get("/reset", response_class=HTMLResponse)
        @app.get("/verify", response_class=HTMLResponse)
        def account_link(request: Request) -> HTMLResponse:
            return page(request, "account.html")  # the token is in the #fragment, read by the page

        @app.get("/app", response_class=HTMLResponse)
        def my_app(request: Request) -> HTMLResponse:
            return page(request, "app.html")  # who you are comes from your session, not the URL

    @app.get("/app/{user_id}", response_class=HTMLResponse, response_model=None)
    def health_app(user_id: str, request: Request) -> HTMLResponse | RedirectResponse:
        if not USER_ID.match(user_id):
            raise HTTPException(status_code=422, detail="invalid user id")
        if auth is not None:
            return RedirectResponse("/app", status_code=303)
        return page(request, "app.html")

    @app.get("/v1/users/{user_id}/mealplan", response_model=MealPlanOut)
    def get_mealplan(user_id: str, start: date | None = None, days: int = 7) -> MealPlanOut:
        if not USER_ID.match(user_id):
            raise HTTPException(status_code=422, detail="invalid user id")
        if not 1 <= days <= 14:
            raise HTTPException(status_code=422, detail="days must be between 1 and 14")
        record = profiles.get(user_id)
        if record is None:
            raise HTTPException(status_code=404, detail="no profile for this user")
        first = start or clock().date()
        try:
            rec = build_meal_plan_recommendation(
                record.profile, record.recent_sleep_hours, str(uuid.uuid4()), first, days
            )
        except PlanError as exc:
            raise HTTPException(
                status_code=409, detail="no meal plan is available for you right now"
            ) from exc
        except OutOfRange as exc:
            raise HTTPException(status_code=422, detail="profile data is not usable") from exc
        if not gate.evaluate(rec).cleared:  # the Guardian is the last step; reasons stay internal
            raise HTTPException(
                status_code=409, detail="no meal plan is available for you right now"
            )
        by_day: dict[date, list[MealOut]] = {}
        for e in rec.meal_plan:
            r = BY_ID[e.recipe_id]
            by_day.setdefault(e.day, []).append(
                MealOut(
                    meal=e.meal,
                    title=r.title,
                    portion=e.scale,
                    kcal=round(r.kcal * e.scale),
                    protein_g=round(r.protein_g * e.scale, 1),
                    carbs_g=round(r.carbs_g * e.scale, 1),
                    fat_g=round(r.fat_g * e.scale, 1),
                )
            )
        out_days = [
            DayOut(day=d, kcal=sum(m.kcal for m in ms), meals=ms)
            for d, ms in sorted(by_day.items())
        ]
        avg = sum(d.kcal for d in out_days) / len(out_days)
        return MealPlanOut(average_daily_kcal=round(avg), days=out_days)

    @app.post("/v1/users/{user_id}/questions", status_code=202, response_model=QuestionOut)
    def post_question(user_id: str, q: QuestionIn, response: Response) -> QuestionOut:
        if not USER_ID.match(user_id):
            raise HTTPException(status_code=422, detail="invalid user id")
        if profiles.get(user_id) is None:
            raise HTTPException(status_code=404, detail="no profile for this user")
        if auth is not None and not auth.verified(user_id):
            raise HTTPException(status_code=403, detail="confirm your email address first")
        if auth is not None and not auth.limiter.hit(f"ask:{user_id}", 20, 3600):
            # each question can spend model quota: the scarcest thing in the system
            raise HTTPException(status_code=429, detail="you have asked a lot this hour; try later")
        category = screen_question(q.question)
        if category is not None:
            response.status_code = 200  # answered, not accepted for processing
            return QuestionOut(status="declined", message=REFUSALS[category])
        correlation_id = str(uuid.uuid4())
        meta = EventMeta(
            correlation_id=correlation_id,
            user_id=user_id,
            household_id="default",
            type="question.asked",
            occurred_at=clock(),
            trace_id=correlation_id,
        )
        payload = payloads_pb2.QuestionAsked(question=q.question, session_id=q.session_id)
        try:
            data = serde.serialize_event(QUESTIONS_ASKED, meta, payload)
            publisher.publish(QUESTIONS_ASKED, user_id, data)
            publisher.flush()
        except EnvelopeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # broker or registry down: tell the client to retry
            raise HTTPException(status_code=503, detail="temporarily unavailable") from exc
        return QuestionOut(status="accepted", correlation_id=correlation_id)

    @app.get("/v1/users/{user_id}/history", response_model=list[QaItem])
    def get_history(user_id: str, limit: int = 10) -> list[QaItem]:
        """The person's recent questions with the answers they were shown (kept in Postgres)."""
        if not USER_ID.match(user_id):
            raise HTTPException(status_code=422, detail="invalid user id")
        if qa is None:
            return []
        return [
            QaItem(
                correlation_id=e.correlation_id,
                question=e.question,
                status=e.status,
                asked_at=e.asked_at,
                answer=answer_of([{"type": "recommendation.released", **e.answer}])
                if e.answer
                else None,
            )
            for e in qa.recent(user_id, max(1, min(limit, KEEP)))
        ]

    def _run_entries(user_id: str, correlation_id: str) -> list[dict[str, Any]]:
        if runs is None:
            raise HTTPException(status_code=404, detail="not enabled")
        if not USER_ID.match(user_id):
            raise HTTPException(status_code=422, detail="invalid user id")
        record = runs.read(correlation_id)
        if record is None or record.user_id != user_id:
            raise HTTPException(status_code=404, detail="no such run")  # not another user's run
        return record.entries

    @app.get("/v1/users/{user_id}/runs/{correlation_id}", response_model=RunView)
    def get_run(user_id: str, correlation_id: str) -> RunView:
        return view_of(_run_entries(user_id, correlation_id))

    @app.get("/v1/users/{user_id}/runs/{correlation_id}/stream")
    def stream_run(user_id: str, correlation_id: str) -> StreamingResponse:
        # The tracker records the first event a moment after the question is accepted, so a run
        # that does not exist YET is waited for briefly; one that never appears is a 404.
        appear_by = time.monotonic() + stream_appear_s
        while True:
            try:
                _run_entries(user_id, correlation_id)
                break
            except HTTPException:
                if time.monotonic() >= appear_by:
                    raise
                time.sleep(stream_poll_s)

        def events() -> Iterator[str]:
            sent, waited = 0, 0.0
            while waited <= stream_max_s:
                entries = _run_entries(user_id, correlation_id)
                view = view_of(entries)
                for step in view.steps[sent:]:
                    yield sse("step", step.model_dump_json())
                sent = len(view.steps)
                if view.status in ("released", "blocked") or (
                    view.status == "needs_review" and sent
                ):
                    yield sse("done", view.model_dump_json())
                    return
                time.sleep(stream_poll_s)
                waited += stream_poll_s
            yield sse("timeout", "{}")

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/v1/ops/runs", response_model=OpsRunsOut)
    def ops_runs(limit: int = 25) -> OpsRunsOut:
        """Recent runs with their flow and timing, and measured latency percentiles over them."""
        if ops is None or runs is None:
            raise HTTPException(status_code=404, detail="not enabled")
        found = runs.recent(200)
        summaries = [summarise_run(cid, rec.entries) for cid, rec in found if rec.entries]
        done = [s for s in summaries if s.status == "released" and s.total_ms is not None]
        tiers = sorted({s.tier or "unknown" for s in done})
        return OpsRunsOut(
            stages_by_tier={
                tier: stage_timings([s for s in done if (s.tier or "unknown") == tier])
                for tier in tiers
            },
            runs=summaries[: max(1, min(limit, 100))],
            latency=percentiles([s.total_ms for s in done if s.total_ms is not None]),
            latency_by_tier={
                tier: percentiles(
                    [
                        s.total_ms
                        for s in done
                        if (s.tier or "unknown") == tier and s.total_ms is not None
                    ]
                )
                for tier in tiers
            },
        )

    @app.get("/v1/ops/runs/{correlation_id}", response_model=OpsRunOut)
    def ops_run(correlation_id: str) -> OpsRunOut:
        """Operator view: one run's events with their internals, minus question and answer text."""
        if ops is None or runs is None:
            raise HTTPException(status_code=404, detail="not enabled")
        record = runs.read(correlation_id)
        if record is None or not record.entries:
            raise HTTPException(status_code=404, detail="no such run")
        return OpsRunOut(
            summary=summarise_run(correlation_id, record.entries), entries=redact(record.entries)
        )

    @app.get("/v1/ops/lag", response_model=list[LagGroup])
    def ops_lag() -> list[LagGroup]:
        """Messages each consumer group has not yet processed, read from the broker."""
        if lag is None:
            raise HTTPException(status_code=404, detail="not enabled")
        try:
            rows = lag()
        except Exception as exc:  # the broker is the thing being asked about: say it is unreachable
            raise HTTPException(status_code=503, detail="the broker did not answer") from exc
        # an agent role has one consumer group per topic ("agent-triage.questions.asked"): fold
        # them into one row per role, and retry tiers into the topic they retry
        by_role: dict[str, list[LagRow]] = {}
        for r in rows:
            by_role.setdefault(r.group.split(".")[0], []).append(r)
        return [
            LagGroup(
                group=role,
                lag=sum(r.lag for r in rs),
                topics=sorted({r.topic.split(".retry.")[0] for r in rs}),
                consumers_down=len({r.group for r in rs if r.members == 0}),
            )
            for role, rs in sorted(by_role.items())
        ]

    @app.get("/v1/ops/graph", response_model=GraphOut)
    def ops_graph() -> GraphOut:
        """The constraint knowledge graph exactly as the agents read it (compiled from Neo4j)."""
        if graph is None:
            raise HTTPException(status_code=404, detail="not enabled")
        found = graph()
        if found is None:
            raise HTTPException(status_code=404, detail="the graph is not loaded: run vitalmesh_kg")
        return found

    @app.get("/v1/ops/evals", response_model=list[EvalRow])
    def ops_evals() -> list[EvalRow]:
        """The committed eval baselines: how many cases each suite passes."""
        if evals is None:
            raise HTTPException(status_code=404, detail="not enabled")
        return evals()

    @app.get("/v1/ops/nudges", response_model=OpsNudgesOut)
    def ops_nudges() -> OpsNudgesOut:
        """How the Engagement agent is deciding: counters and the model-vs-rule agreement."""
        if nudges is None:
            raise HTTPException(status_code=404, detail="not enabled")
        c = nudges.stats()
        asked = c.get("cache_hit", 0) + c.get("cache_miss", 0)
        compared = c.get("agree", 0) + c.get("disagree", 0)
        by_model = compared  # agree/disagree are counted for the model's own decisions only
        return OpsNudgesOut(
            counters=c,
            decisions_by_model=by_model,
            model_agreement=round(c.get("agree", 0) / compared, 3) if compared else None,
            cache_hit_share=round(c.get("cache_hit", 0) / asked, 3) if asked else None,
            vetoed_by_delivery=sum(v for k, v in c.items() if k.startswith("delivery_vetoed_")),
        )

    @app.get("/v1/ops/tiers", response_model=OpsTiersOut)
    def ops_tiers() -> OpsTiersOut:
        """Operator view of how runs were planned (read-only, counts only, no user data)."""
        if ops is None:
            raise HTTPException(status_code=404, detail="not enabled")
        data = ops()
        tiers, cohort = data.get("tiers", {}), data.get("cohort", {})
        planned = sum(tiers.values())
        return OpsTiersOut(
            tiers=tiers,
            cohort=cohort,
            planned=planned,
            cache_hit_share=round(tiers.get("T1", 0) / planned, 3) if planned else None,
            no_model_share=(
                round((tiers.get("T0", 0) + tiers.get("T1", 0)) / planned, 3) if planned else None
            ),
        )

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
