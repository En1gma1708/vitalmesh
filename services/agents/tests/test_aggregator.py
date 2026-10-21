from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from vitalmesh.events.v1 import common_pb2, payloads_pb2
from vitalmesh_agents.aggregator import Aggregator, bundle_event_id
from vitalmesh_agents.events import POST_BUNDLE_BUDGET_S
from vitalmesh_agents.runstore import InMemoryRunStore
from vitalmesh_common import codec
from vitalmesh_common.envelope import EventMeta, Tier
from vitalmesh_common.testing import PlainSerde, RecordingPublisher
from vitalmesh_common.topics import ADVICE_BUNDLES

T0 = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
DEADLINE = T0 + timedelta(seconds=90)
NUT, MOV, CON = (
    common_pb2.SPECIALIST_NUTRITION,
    common_pb2.SPECIALIST_MOVEMENT_RECOVERY,
    common_pb2.SPECIALIST_CONDITION_MEDICATION,
)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def meta(type_: str, cid: str = "run-1", event_id: str | None = None, **extra: Any) -> EventMeta:
    return EventMeta(
        **({"event_id": event_id} if event_id else {}),
        correlation_id=cid,
        user_id="u-1",
        household_id="h-1",
        type=type_,
        occurred_at=T0,
        trace_id="trace-1",
        tier=Tier.T3,
        **extra,
    )


def plan(*specialists: int, cid: str = "run-1") -> tuple[EventMeta, Any]:
    msg = payloads_pb2.RunPlanned(question="What now?", risk=common_pb2.RISK_MEDIUM)
    msg.specialists.extend(specialists)
    return meta("run.planned", cid, f"plan-{cid}", deadline_at=DEADLINE), msg


def part(
    kind: int, status: int = payloads_pb2.ADVICE_ANSWERED, cid: str = "run-1"
) -> tuple[EventMeta, Any]:
    msg = payloads_pb2.AdviceCompleted(specialist=kind, status=status, text=f"advice {kind}")
    return meta("advice.completed", cid, f"advice-{cid}-{kind}", deadline_at=DEADLINE), msg


def setup() -> tuple[Aggregator, InMemoryRunStore, RecordingPublisher, Clock]:
    store, pub, clock = InMemoryRunStore(), RecordingPublisher(), Clock()
    return Aggregator(store, PlainSerde(), pub, clock), store, pub, clock


def bundles(pub: RecordingPublisher) -> list[tuple[EventMeta, Any]]:
    return [codec.decode(p.value) for p in pub.sent if p.topic == ADVICE_BUNDLES]


def test_all_parts_arrive_so_one_complete_bundle_is_published() -> None:
    agg, _, pub, clock = setup()
    clock.now = T0 + timedelta(seconds=20)
    for event in (plan(NUT, MOV), part(MOV), part(NUT)):
        agg.handle(*event)
    ((meta_out, bundle),) = bundles(pub)
    assert [p.topic for p in pub.sent] == [ADVICE_BUNDLES]
    assert pub.log == ["publish", "flush"]
    assert (bundle.reason, bundle.partial, list(bundle.missing)) == ("complete", False, [])
    assert [p.specialist for p in bundle.parts] == [NUT, MOV]  # canonical order, not arrival order
    assert bundle.question == "What now?" and bundle.risk == common_pb2.RISK_MEDIUM
    assert meta_out.event_id == bundle_event_id("run-1")
    assert meta_out.correlation_id == "run-1" and meta_out.user_id == "u-1"
    assert meta_out.trace_id == "trace-1" and meta_out.tier is Tier.T3
    assert meta_out.causation_id == "plan-run-1"
    # downstream gets its own budget: this event may be emitted AT the run deadline
    assert meta_out.deadline_at == clock.now + timedelta(seconds=POST_BUNDLE_BUDGET_S)


@pytest.mark.parametrize("order", list(itertools.permutations(range(4))))
def test_any_arrival_order_gives_exactly_one_complete_bundle(order: tuple[int, ...]) -> None:
    events = [plan(NUT, MOV, CON), part(NUT), part(MOV), part(CON)]
    agg, _, pub, _ = setup()
    for i in order:
        agg.handle(*events[i])
    ((_, bundle),) = bundles(pub)
    assert len(bundle.parts) == 3 and not bundle.partial


def test_a_missing_part_closes_partial_at_the_deadline_and_not_before() -> None:
    agg, _, pub, clock = setup()
    agg.handle(*plan(NUT, MOV))
    agg.handle(*part(NUT))
    clock.now = DEADLINE - timedelta(seconds=1)
    assert agg.sweep() == 0 and bundles(pub) == []
    clock.now = DEADLINE + timedelta(seconds=1)
    assert agg.sweep() == 1
    ((meta_out, bundle),) = bundles(pub)
    assert (bundle.reason, bundle.partial) == ("deadline", True)
    assert list(bundle.missing) == [MOV]
    assert [p.specialist for p in bundle.parts] == [NUT]
    assert meta_out.deadline_at is not None and meta_out.deadline_at > DEADLINE  # not expired
    assert agg.sweep() == 0  # closed: nothing more happens


def test_a_failed_part_counts_as_arrived_so_the_run_does_not_wait_but_is_reported_missing() -> None:
    agg, _, pub, _ = setup()
    agg.handle(*plan(NUT, MOV))
    agg.handle(*part(NUT))
    agg.handle(*part(MOV, payloads_pb2.ADVICE_FAILED))
    ((_, bundle),) = bundles(pub)  # closed at once, not at the deadline
    assert bundle.reason == "complete" and bundle.partial
    assert list(bundle.missing) == [MOV]
    assert len(bundle.parts) == 2 and bundle.parts[1].status == payloads_pb2.ADVICE_FAILED


def test_redelivery_does_not_publish_a_second_bundle() -> None:
    agg, _, pub, _ = setup()
    events = [plan(NUT), part(NUT)]
    for event in events:
        agg.handle(*event)
    for event in events:  # at-least-once: the same events again
        agg.handle(*event)
    assert len(bundles(pub)) == 1


def test_a_late_part_after_closing_is_dropped() -> None:
    agg, _, pub, clock = setup()
    agg.handle(*plan(NUT, MOV))
    clock.now = DEADLINE + timedelta(seconds=1)
    agg.sweep()
    agg.handle(*part(MOV))  # arrives too late
    agg.sweep()
    assert len(bundles(pub)) == 1 and bundles(pub)[0][1].partial


def test_a_part_from_a_specialist_the_plan_did_not_name_is_ignored() -> None:
    agg, _, pub, _clock = setup()
    agg.handle(*plan(NUT))
    agg.handle(*part(CON))  # zero trust: not in the plan
    assert bundles(pub) == []  # still waiting for nutrition
    agg.handle(*part(NUT))
    ((_, bundle),) = bundles(pub)
    assert [p.specialist for p in bundle.parts] == [NUT]


def test_a_failed_publish_keeps_the_run_open_so_the_next_event_or_sweep_retries() -> None:
    agg, store, pub, clock = setup()
    agg.handle(*plan(NUT))
    pub.fail_flush = True
    with pytest.raises(RuntimeError):
        agg.handle(*part(NUT))
    assert not store.is_closed("run-1")
    pub.fail_flush = False
    clock.now = DEADLINE + timedelta(seconds=1)
    assert agg.sweep() == 1
    assert store.is_closed("run-1")
    # both attempts used the same deterministic id, so a consumer would drop the duplicate
    ids = {codec.decode(p.value)[0].event_id for p in pub.sent}
    assert ids == {bundle_event_id("run-1")}


def test_a_competing_closer_makes_this_one_back_off() -> None:
    agg, store, pub, clock = setup()
    agg.handle(*plan(NUT))
    assert store.try_close("run-1")  # another worker is closing it
    agg.handle(*part(NUT))
    assert bundles(pub) == []
    store.release_close("run-1")  # that worker died: the sweep picks the run up
    clock.now = DEADLINE + timedelta(seconds=1)
    assert agg.sweep() == 1
    assert len(bundles(pub)) == 1


def test_a_plan_without_a_deadline_is_rejected_because_it_could_never_time_out() -> None:
    agg, _, _, _ = setup()
    msg = payloads_pb2.RunPlanned(question="q", risk=common_pb2.RISK_LOW)
    msg.specialists.append(NUT)
    with pytest.raises(ValueError):
        agg.handle(meta("run.planned"), msg)


def test_an_unspecified_specialist_or_risk_is_an_invalid_event() -> None:
    from vitalmesh_common.failure import EnvelopeError

    agg, _, _, _ = setup()
    bad = payloads_pb2.RunPlanned(question="q")  # risk unspecified
    bad.specialists.append(NUT)
    with pytest.raises(EnvelopeError):
        agg.handle(meta("run.planned", deadline_at=DEADLINE), bad)
    with pytest.raises(EnvelopeError):
        agg.handle(*part(common_pb2.SPECIALIST_UNSPECIFIED))


def test_runs_are_independent() -> None:
    agg, _, pub, _ = setup()
    agg.handle(*plan(NUT, cid="a"))
    agg.handle(*plan(MOV, cid="b"))
    agg.handle(*part(MOV, cid="b"))
    out = bundles(pub)
    assert len(out) == 1 and out[0][0].correlation_id == "b"


def test_a_plan_with_no_specialists_closes_at_once_instead_of_hanging() -> None:
    agg, _, pub, _ = setup()
    agg.handle(*plan())
    ((_, bundle),) = bundles(pub)
    assert len(bundle.parts) == 0 and bundle.reason == "complete"


def test_other_event_types_are_ignored() -> None:
    agg, store, pub, _ = setup()
    agg.handle(meta("recommendation.drafted"), payloads_pb2.RecommendationDrafted())
    assert pub.sent == [] and store.snapshot("run-1").plan is None
