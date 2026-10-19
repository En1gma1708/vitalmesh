"""Structured failure contract (docs/EVENT_BACKBONE.md section 11).

Every ``*.failed`` event and DLQ record carries one ``Failure``. The contract is
fail-closed: categories that can never succeed on retry (invalid input, integrity
mismatch, safety block, cancellation) are rejected at construction if marked retryable.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FailureCategory(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_MISMATCH = "integrity_mismatch"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    RESOURCE_CONSTRAINT = "resource_constraint"
    UNAVAILABLE = "unavailable"
    EXECUTION_FAILURE = "execution_failure"
    SAFETY_BLOCKED = "safety_blocked"


# Retrying these can never change the outcome, so they must not be marked retryable.
NEVER_RETRYABLE: frozenset[FailureCategory] = frozenset(
    {
        FailureCategory.INVALID_INPUT,
        FailureCategory.INTEGRITY_MISMATCH,
        FailureCategory.CANCELLED,
        FailureCategory.SAFETY_BLOCKED,
    }
)


class Remediation(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: str = Field(min_length=1)


class Failure(BaseModel):
    """What failed, whether retrying can help, and what to do about it."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1)
    category: FailureCategory
    message: str = Field(min_length=1)
    retryable: bool
    remediation: Remediation
    # Partial facts computed before the failure are preserved, never discarded.
    details: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _fail_closed(self) -> Failure:
        if self.retryable and self.category in NEVER_RETRYABLE:
            raise ValueError(f"category {self.category.value!r} must not be marked retryable")
        return self


class EnvelopeError(ValueError):
    """An event envelope is malformed or violates the contract (never retryable)."""


class DeadlineExpired(TimeoutError):
    """An event's deadline passed before it could be processed."""


def failure_from_exception(exc: BaseException) -> Failure:
    """Classify an exception into the failure contract.

    Unknown exceptions are ``execution_failure`` and retryable: retries are bounded by the
    retry policy, and a poison message ends up in the DLQ rather than blocking a partition.
    """
    if isinstance(exc, EnvelopeError):
        return Failure(
            code="envelope.invalid",
            category=FailureCategory.INVALID_INPUT,
            message=str(exc) or "invalid envelope",
            retryable=False,
            remediation=Remediation(action="Fix the producer so it emits a valid envelope."),
        )
    if isinstance(exc, DeadlineExpired):
        return Failure(
            code="deadline.expired",
            category=FailureCategory.TIMEOUT,
            message=str(exc) or "deadline expired before processing",
            retryable=False,
            remediation=Remediation(action="Fall back to the cached or deterministic result."),
        )
    if isinstance(exc, TimeoutError):
        return Failure(
            code="handler.timeout",
            category=FailureCategory.TIMEOUT,
            message=str(exc) or "handler timed out",
            retryable=True,
            remediation=Remediation(action="Retry with backoff; check the downstream dependency."),
        )
    if isinstance(exc, ConnectionError):
        return Failure(
            code="dependency.unavailable",
            category=FailureCategory.UNAVAILABLE,
            message=str(exc) or "dependency unavailable",
            retryable=True,
            remediation=Remediation(action="Retry with backoff; check dependency health."),
        )
    return Failure(
        code="handler.error",
        category=FailureCategory.EXECUTION_FAILURE,
        message=f"{type(exc).__name__}: {exc}"[:500] or type(exc).__name__,
        retryable=True,
        remediation=Remediation(action="Inspect the DLQ record and the handler logs."),
    )
