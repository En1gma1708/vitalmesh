"""Event envelope metadata (docs/EVENT_BACKBONE.md section 3).

This is the pure-domain view of the envelope. The wire format is the Protobuf
``Envelope`` message (proto/vitalmesh/events/v1/envelope.proto); ``codec`` converts
between the two at the adapter boundary.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vitalmesh_common.failure import DeadlineExpired, EnvelopeError


class Tier(StrEnum):
    """Compute tier chosen by Triage (docs/ARCHITECTURE.md section 2)."""

    T0 = "T0"  # deterministic, no LLM
    T1 = "T1"  # cohort cache
    T2 = "T2"  # single small agent
    T3 = "T3"  # full multi-agent graph


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class EventMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=new_id, min_length=1)
    correlation_id: str = Field(min_length=1)  # one recommendation run
    causation_id: str | None = None  # event that caused this one
    user_id: str = Field(min_length=1)  # Kafka partition key
    household_id: str = Field(min_length=1)  # tenancy
    # Provider dedupe: the idempotency key is (source, external_id) when present.
    source: str | None = None
    external_id: str | None = None
    type: str = Field(min_length=1)
    schema_version: int = Field(ge=1, default=1)
    occurred_at: datetime
    trace_id: str | None = None
    tier: Tier | None = None
    deadline_at: datetime | None = None
    attempt: int = Field(ge=0, default=0)  # delivery attempts so far (retry topics)
    rules_version: str | None = None  # Guardian rules version that checked this event

    @model_validator(mode="after")
    def _validate(self) -> EventMeta:
        if (self.source is None) != (self.external_id is None):
            raise EnvelopeError("source and external_id must be set together")
        if self.source is not None and not self.source:
            raise EnvelopeError("source must not be empty")
        if self.occurred_at.tzinfo is None:
            raise EnvelopeError("occurred_at must be timezone-aware")
        if self.deadline_at is not None:
            if self.deadline_at.tzinfo is None:
                raise EnvelopeError("deadline_at must be timezone-aware")
            if self.deadline_at < self.occurred_at:
                raise EnvelopeError("deadline_at must not precede occurred_at")
        return self

    def idempotency_key(self) -> str:
        """(source, external_id) when the provider supplied one, else the event id."""
        if self.source is not None and self.external_id is not None:
            return f"{self.source}:{self.external_id}"
        return self.event_id

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.deadline_at is not None and (now or utcnow()) > self.deadline_at

    def ensure_not_expired(self, now: datetime | None = None) -> None:
        if self.is_expired(now):
            raise DeadlineExpired(f"event {self.event_id} expired at {self.deadline_at}")

    def next_attempt(self) -> EventMeta:
        return self.model_copy(update={"attempt": self.attempt + 1})
