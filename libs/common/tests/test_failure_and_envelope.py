from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from vitalmesh_common.envelope import EventMeta, Tier
from vitalmesh_common.failure import (
    NEVER_RETRYABLE,
    DeadlineExpired,
    EnvelopeError,
    Failure,
    FailureCategory,
    Remediation,
    failure_from_exception,
)

T0 = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


def make_meta(**overrides: object) -> EventMeta:
    base: dict[str, object] = {
        "correlation_id": "corr-1",
        "user_id": "u-1",
        "household_id": "h-1",
        "type": "measurement.recorded",
        "occurred_at": T0,
    }
    base.update(overrides)
    return EventMeta(**base)


# ---------------------------------------------------------------- failure contract
@pytest.mark.parametrize("category", sorted(NEVER_RETRYABLE))
def test_never_retryable_categories_reject_retryable_true(category: FailureCategory) -> None:
    with pytest.raises(ValidationError):
        Failure(
            code="x",
            category=category,
            message="m",
            retryable=True,
            remediation=Remediation(action="a"),
        )


@pytest.mark.parametrize(
    "category",
    [c for c in FailureCategory if c not in NEVER_RETRYABLE],
)
def test_other_categories_may_be_retryable(category: FailureCategory) -> None:
    f = Failure(
        code="x",
        category=category,
        message="m",
        retryable=True,
        remediation=Remediation(action="a"),
    )
    assert f.retryable


def test_remediation_is_required_and_non_empty() -> None:
    with pytest.raises(ValidationError):
        Failure(
            code="x",
            category=FailureCategory.TIMEOUT,
            message="m",
            retryable=True,
            remediation=Remediation(action=""),
        )


@pytest.mark.parametrize(
    ("exc", "category", "retryable"),
    [
        (EnvelopeError("bad"), FailureCategory.INVALID_INPUT, False),
        (DeadlineExpired("late"), FailureCategory.TIMEOUT, False),
        (TimeoutError("slow"), FailureCategory.TIMEOUT, True),
        (ConnectionError("down"), FailureCategory.UNAVAILABLE, True),
        (RuntimeError("boom"), FailureCategory.EXECUTION_FAILURE, True),
    ],
)
def test_failure_from_exception_classification(
    exc: BaseException, category: FailureCategory, retryable: bool
) -> None:
    f = failure_from_exception(exc)
    assert f.category is category
    assert f.retryable is retryable
    assert f.remediation.action


def test_deadline_expired_is_not_confused_with_plain_timeout() -> None:
    # DeadlineExpired subclasses TimeoutError; it must still be non-retryable.
    assert not failure_from_exception(DeadlineExpired("x")).retryable


def test_unknown_exception_message_is_bounded() -> None:
    f = failure_from_exception(RuntimeError("x" * 5000))
    assert len(f.message) <= 500


# --------------------------------------------------------------------- envelope
def test_idempotency_key_prefers_provider_identity() -> None:
    m = make_meta(source="fitbit", external_id="abc-123")
    assert m.idempotency_key() == "fitbit:abc-123"


def test_idempotency_key_falls_back_to_event_id() -> None:
    m = make_meta()
    assert m.idempotency_key() == m.event_id


def test_source_and_external_id_must_come_together() -> None:
    with pytest.raises(ValidationError):
        make_meta(source="fitbit")
    with pytest.raises(ValidationError):
        make_meta(external_id="abc")


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValidationError):
        make_meta(occurred_at=datetime(2026, 10, 20, 8, 0))
    with pytest.raises(ValidationError):
        make_meta(deadline_at=datetime(2026, 10, 20, 9, 0))


def test_deadline_must_not_precede_occurrence() -> None:
    with pytest.raises(ValidationError):
        make_meta(deadline_at=T0 - timedelta(seconds=1))


def test_expiry_boundaries() -> None:
    m = make_meta(deadline_at=T0 + timedelta(seconds=10))
    assert not m.is_expired(T0 + timedelta(seconds=10))  # at the deadline: still valid
    assert m.is_expired(T0 + timedelta(seconds=11))
    assert not make_meta().is_expired(T0 + timedelta(days=3650))  # no deadline: never expires


def test_ensure_not_expired_raises_deadline_expired() -> None:
    m = make_meta(deadline_at=T0 + timedelta(seconds=1))
    with pytest.raises(DeadlineExpired):
        m.ensure_not_expired(T0 + timedelta(seconds=2))


def test_next_attempt_is_a_new_object() -> None:
    m = make_meta(tier=Tier.T0)
    n = m.next_attempt()
    assert (m.attempt, n.attempt) == (0, 1)
    assert n.event_id == m.event_id  # the same event, one more delivery attempt


def test_required_identity_fields_cannot_be_empty() -> None:
    for field in ("correlation_id", "user_id", "household_id", "type"):
        with pytest.raises(ValidationError):
            make_meta(**{field: ""})
