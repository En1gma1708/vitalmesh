from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.failure import (
    Failure,
    FailureCategory,
    Remediation,
    failure_from_exception,
)
from vitalmesh_common.idempotency import (
    DONE_TTL_S,
    IN_FLIGHT_TTL_S,
    Acquired,
    InMemoryIdempotencyStore,
)
from vitalmesh_common.retry import DlqRoute, RetryPolicy, RetryRoute, route_failure
from vitalmesh_common.topics import (
    TOPICS,
    USER_EVENTS,
    Profile,
    all_topic_specs,
    base_topic,
    dlq_topic,
    retry_topic,
)

NOW = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


def meta(attempt: int = 0, deadline: datetime | None = None) -> EventMeta:
    return EventMeta(
        correlation_id="c",
        user_id="u",
        household_id="h",
        type="measurement.recorded",
        occurred_at=NOW - timedelta(minutes=1),
        attempt=attempt,
        deadline_at=deadline,
    )


RETRYABLE = failure_from_exception(RuntimeError("boom"))
NON_RETRYABLE = Failure(
    code="x",
    category=FailureCategory.INVALID_INPUT,
    message="bad",
    retryable=False,
    remediation=Remediation(action="fix"),
)


# ------------------------------------------------------------------------ topics
def test_retry_and_dlq_names_round_trip() -> None:
    assert retry_topic(USER_EVENTS, 1) == "user.events.retry.1"
    assert dlq_topic(USER_EVENTS) == "user.events.dlq"
    for name in (USER_EVENTS, retry_topic(USER_EVENTS, 2), dlq_topic(USER_EVENTS)):
        assert base_topic(name) == USER_EVENTS


def test_retry_tiers_are_one_based() -> None:
    with pytest.raises(ValueError):
        retry_topic(USER_EVENTS, 0)


def test_a_topic_that_merely_contains_retry_is_not_mangled() -> None:
    assert base_topic("user.retry.notes") == "user.retry.notes"
    assert base_topic("user.events.retry.x") == "user.events.retry.x"  # tier must be numeric


def test_every_topic_gets_one_retry_topic_per_tier_and_a_dlq() -> None:
    tiers = RetryPolicy().max_retries
    specs = all_topic_specs(tiers)
    assert len(specs) == (tiers + 2) * len(TOPICS)  # main + N retry tiers + dlq
    names = {s.name for s in specs}
    for base in TOPICS:
        expected = {base, dlq_topic(base)} | {retry_topic(base, t) for t in range(1, tiers + 1)}
        assert expected <= names


def test_dlq_retains_longer_than_the_main_topic() -> None:
    by_name = {s.name: s for s in all_topic_specs()}
    assert by_name[dlq_topic(USER_EVENTS)].retention_ms > by_name[USER_EVENTS].retention_ms


def test_user_events_partitioning_profiles() -> None:
    spec = TOPICS[USER_EVENTS]
    assert spec.key == "user_id"  # per-user ordering
    assert spec.partitions_for(Profile.LOCAL) == 6
    assert spec.partitions_for(Profile.PRODUCTION) == 96  # documented capacity target
    assert spec.replication_for(Profile.LOCAL) == 1
    assert spec.replication_for(Profile.PRODUCTION) == 3


# ------------------------------------------------------------------------- retry
def test_retryable_failure_goes_to_retry_with_backoff() -> None:
    r = route_failure(USER_EVENTS, meta(attempt=0), RETRYABLE, now=NOW)
    assert isinstance(r, RetryRoute)
    assert r.topic == "user.events.retry.1"
    assert r.next_attempt == 1
    assert r.not_before == NOW + timedelta(seconds=5)


def test_backoff_grows_with_each_attempt() -> None:
    policy = RetryPolicy()
    delays = []
    for attempt in range(policy.max_retries):
        r = route_failure(
            retry_topic(USER_EVENTS, attempt) if attempt else USER_EVENTS,
            meta(attempt=attempt),
            RETRYABLE,
            policy,
            NOW,
        )
        assert isinstance(r, RetryRoute)
        assert r.topic == retry_topic(USER_EVENTS, attempt + 1)  # tier n+1 after n retries
        delays.append(r.not_before - NOW)
    assert delays == list(policy.backoff)
    assert delays == sorted(delays)


def test_retry_budget_exhaustion_goes_to_dlq() -> None:
    policy = RetryPolicy()
    r = route_failure(
        retry_topic(USER_EVENTS, policy.max_retries),
        meta(attempt=policy.max_retries),
        RETRYABLE,
        policy,
        NOW,
    )
    assert isinstance(r, DlqRoute)
    assert r.topic == "user.events.dlq"  # origin topic, not a retry-tier dlq
    assert "exhausted" in r.reason


def test_non_retryable_goes_straight_to_dlq() -> None:
    r = route_failure(USER_EVENTS, meta(), NON_RETRYABLE, now=NOW)
    assert isinstance(r, DlqRoute)
    assert "invalid_input" in r.reason


def test_expired_event_is_never_retried_even_if_failure_is_retryable() -> None:
    expired = meta(deadline=NOW - timedelta(seconds=1))
    r = route_failure(USER_EVENTS, expired, RETRYABLE, now=NOW)
    assert isinstance(r, DlqRoute)
    assert "expired" in r.reason


def test_custom_policy_with_zero_retries_always_dlqs() -> None:
    r = route_failure(USER_EVENTS, meta(), RETRYABLE, RetryPolicy(backoff=()), NOW)
    assert isinstance(r, DlqRoute)


# ------------------------------------------------------------------- idempotency
class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_first_acquire_is_new_then_in_flight() -> None:
    s = InMemoryIdempotencyStore(Clock())
    assert s.acquire("g", "k") is Acquired.NEW
    assert s.acquire("g", "k") is Acquired.IN_FLIGHT


def test_commit_marks_done() -> None:
    s = InMemoryIdempotencyStore(Clock())
    s.acquire("g", "k")
    s.commit("g", "k")
    assert s.acquire("g", "k") is Acquired.DONE


def test_release_lets_a_retry_reprocess() -> None:
    """The reason the protocol is three-state: a failed handler must not poison the key."""
    s = InMemoryIdempotencyStore(Clock())
    assert s.acquire("g", "k") is Acquired.NEW
    s.release("g", "k")  # handler failed
    assert s.acquire("g", "k") is Acquired.NEW  # the retry is processed, not dropped


def test_crashed_worker_does_not_lock_the_key_forever() -> None:
    clock = Clock()
    s = InMemoryIdempotencyStore(clock)
    assert s.acquire("g", "k") is Acquired.NEW  # worker dies without commit/release
    clock.t += IN_FLIGHT_TTL_S + 1
    assert s.acquire("g", "k") is Acquired.NEW


def test_done_expires_after_its_ttl() -> None:
    clock = Clock()
    s = InMemoryIdempotencyStore(clock)
    s.acquire("g", "k")
    s.commit("g", "k")
    clock.t += DONE_TTL_S + 1
    assert s.acquire("g", "k") is Acquired.NEW


def test_namespaces_are_isolated_per_consumer_group() -> None:
    s = InMemoryIdempotencyStore(Clock())
    s.acquire("state-processor", "k")
    s.commit("state-processor", "k")
    assert s.acquire("triage", "k") is Acquired.NEW  # a different consumer still processes it


@pytest.mark.parametrize("n", [1, 5])
def test_commit_is_idempotent(n: int) -> None:
    s = InMemoryIdempotencyStore(Clock())
    s.acquire("g", "k")
    for _ in range(n):
        s.commit("g", "k")
    assert s.acquire("g", "k") is Acquired.DONE
