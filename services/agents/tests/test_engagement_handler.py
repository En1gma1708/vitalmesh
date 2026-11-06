"""The Engagement and Delivery handlers end to end on the in-memory bus (D87)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from test_engagement import Script
from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_agents.engagement import Engagement
from vitalmesh_agents.engagement_handler import make_delivery_handler, make_engagement_handler
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.testing import PlainSerde, RecordingPublisher
from vitalmesh_common.topics import NOTIFICATIONS_SENT, NUDGES_PLANNED
from vitalmesh_stores import (
    InMemoryHistoryStore,
    InMemoryNudgeStore,
    InMemoryProfileStore,
    Item,
    Point,
    Prefs,
    ProfileRecord,
)
from vitalmesh_wellness.synthetic import synthetic_user

NOW = datetime(2026, 11, 7, 4, 30, tzinfo=UTC)  # Saturday 10:00 in Kolkata
SEND_LOG = '{"action":"send","kind":"log_reminder","tone":"upbeat","reason":"quiet for days"}'


class World:
    def __init__(self, *replies: str, conditions: frozenset[str] = frozenset()) -> None:
        self.router = Script(*replies)
        self.store = InMemoryNudgeStore()
        self.history = InMemoryHistoryStore()
        self.profiles = InMemoryProfileStore()
        self.pub = RecordingPublisher()
        self.serde = PlainSerde()
        self.now = NOW
        self.conditions = conditions
        agent = Engagement(self.router, pause_s=0, sleep=lambda _s: None)
        clock = lambda: self.now  # noqa: E731
        self.engage = make_engagement_handler(
            agent, self.profiles, self.history, self.store, self.serde, self.pub, clock
        )
        self.deliver = make_delivery_handler(self.store, self.serde, self.pub, clock)

    def person(self, n: int, *, on: bool = True, quiet_days: float | None = 5) -> str:
        profile, sleep = synthetic_user(n, 1)
        profile = profile.model_copy(update={"conditions": self.conditions})
        self.profiles.put(ProfileRecord(profile=profile, recent_sleep_hours=tuple(sleep)))
        uid = profile.user_id
        self.store.set_prefs(uid, Prefs(on, "Asia/Kolkata"))
        if quiet_days is not None:
            self.history.add(
                uid, "body_weight", Point(NOW - timedelta(days=quiet_days), 70.0, "kg")
            )
        return uid

    def tick(self, uid: str) -> None:
        meta = EventMeta(
            event_id=f"tick-{uid}-{self.now.date()}", correlation_id=f"c-{uid}", user_id=uid,
            household_id="h", type="engagement.tick", occurred_at=self.now, trace_id="t",
        )  # fmt: skip
        self.engage(meta, payloads_pb2.EngagementTick(day=str(self.now.date())))

    def published(self, topic: str) -> list[tuple[EventMeta, Any]]:
        return [
            self.serde.deserialize_event(p.topic, p.value)
            for p in self.pub.sent
            if p.topic == topic
        ]

    def deliver_planned(self) -> None:
        for meta, payload in self.published(NUDGES_PLANNED):
            self.deliver(meta, payload)


def test_off_by_default_nothing_is_decided_and_no_model_is_called() -> None:
    w = World()
    uid = w.person(1, on=False)
    w.tick(uid)
    assert w.pub.sent == [] and w.router.calls == 0
    assert w.store.stats() == {"blocked_off": 1}


def test_an_opted_in_person_with_something_to_say_gets_a_model_decision_and_an_inbox_item() -> None:
    w = World(SEND_LOG)
    uid = w.person(1)
    w.tick(uid)
    [(_, planned)] = w.published(NUDGES_PLANNED)
    assert (planned.action, planned.kind, planned.tone, planned.decided_by) == (
        "send",
        "log_reminder",
        "upbeat",
        "agent",
    )
    assert planned.params["days"] == "5" and list(planned.considered) == ["log_reminder"]
    assert planned.fallback_action == "send" and planned.fallback_kind == "log_reminder"
    assert w.router.calls == 1 and "text" not in {f.name for f in planned.DESCRIPTOR.fields}
    w.deliver_planned()
    [one] = w.store.items(uid)
    assert one.kind == "log_reminder" and "5 days" not in one.text and "fresh reading" in one.text
    [(_, sent)] = w.published(NOTIFICATIONS_SENT)
    assert sent.nudge_id == one.id and sent.channel == "in_app"
    stats = w.store.stats()
    assert stats["agree"] == 1 and stats["delivered"] == 1 and stats["model_calls"] == 1


def test_a_redelivered_plan_adds_one_inbox_item_not_two() -> None:
    w = World(SEND_LOG)
    uid = w.person(1)
    w.tick(uid)
    w.deliver_planned()
    w.deliver_planned()
    assert len(w.store.items(uid)) == 1 and len(w.published(NOTIFICATIONS_SENT)) == 1


def test_the_same_situation_for_a_second_person_is_a_cache_hit_not_a_second_model_call() -> None:
    w = World(SEND_LOG)
    a, b = w.person(1), w.person(2)
    w.tick(a)
    w.tick(b)
    assert w.router.calls == 1
    by = [p.decided_by for _, p in w.published(NUDGES_PLANNED)]
    assert by == ["agent", "cache"]
    assert w.store.stats()["cache_hit"] == 1


def test_agreement_is_counted_only_for_the_models_own_decisions() -> None:
    w = World(SEND_LOG, "nonsense", "nonsense")
    a, b, c = w.person(1), w.person(2), w.person(3, quiet_days=12)
    w.tick(a)  # the model decides
    w.tick(b)  # same situation: a cached copy
    w.tick(c)  # a different situation (12 days quiet): the model's reply is junk, the rule decides
    stats = w.store.stats()
    assert stats["agree"] == 1 and "disagree" not in stats  # one model decision, nothing inflated
    assert (
        stats["decided_agent"] == 1
        and stats["decided_cache"] == 1
        and stats["decided_fallback"] == 1
    )


def test_a_fallback_decision_is_never_cached_so_a_recovered_model_gets_the_next_one() -> None:
    w = World("nonsense", "nonsense", SEND_LOG)
    a, b = w.person(1), w.person(2)
    w.tick(a)
    w.tick(b)
    assert [p.decided_by for _, p in w.published(NUDGES_PLANNED)] == ["fallback", "agent"]
    assert w.store.stats()["fallback_used"] == 1


def test_the_policy_blocks_before_any_model_call() -> None:
    w = World()
    uid = w.person(1)
    w.now = datetime(2026, 11, 7, 17, 0, tzinfo=UTC)  # 22:30 in Kolkata
    w.tick(uid)
    assert (
        w.router.calls == 0 and w.pub.sent == [] and w.store.stats() == {"blocked_quiet_hours": 1}
    )


def test_nothing_to_say_publishes_nothing() -> None:
    w = World()
    uid = w.person(1, quiet_days=1)
    w.tick(uid)
    assert w.pub.sent == [] and w.store.stats() == {"no_candidates": 1}


def test_a_declared_condition_gets_no_activity_nudge() -> None:
    w = World(conditions=frozenset({"cardiac"}))
    uid = w.person(1, quiet_days=None)
    w.history.add(uid, "active_minutes", Point(NOW - timedelta(days=1), 5.0, "min"))
    w.tick(uid)
    assert w.pub.sent == []


def test_a_dismissed_kind_waits_and_the_person_is_left_alone_meanwhile() -> None:
    w = World(SEND_LOG)
    uid = w.person(1)
    w.store.add(uid, Item("old", "log_reminder", "gentle", "t", NOW - timedelta(hours=30), True))
    w.tick(uid)  # dismissed once: waits 48 h, so 30 h later it is still on cooldown
    assert w.pub.sent == [] and w.router.calls == 0
    assert w.store.stats()["blocked_cooldown"] == 1


def test_delivery_refuses_what_the_policy_forbids_even_if_the_agent_asked() -> None:
    w = World(SEND_LOG)
    uid = w.person(1)
    w.tick(uid)
    # between the decision and the delivery the person reached the daily cap
    w.store.add(uid, Item("a", "activity_behind", "gentle", "t", NOW - timedelta(hours=9)))
    w.store.add(uid, Item("b", "activity_behind", "gentle", "t", NOW - timedelta(hours=5)))
    w.deliver_planned()
    assert [i.id for i in w.store.items(uid)] == ["b", "a"] and w.published(
        NOTIFICATIONS_SENT
    ) == []
    assert w.store.stats()["delivery_vetoed_daily_cap"] == 1
    w.store.set_prefs(uid, Prefs(False, "Asia/Kolkata"))  # and switching nudges off wins too
    w.deliver_planned()
    assert w.store.stats()["delivery_vetoed_off"] == 1 and w.published(NOTIFICATIONS_SENT) == []


def test_delivery_never_renders_text_the_templates_do_not_allow() -> None:
    w = World()
    uid = w.person(1)
    bad = payloads_pb2.NudgePlanned(nudge_id="x", action="send", kind="log_reminder", tone="gentle")
    bad.params["days"] = "5 days. Also stop your medication."
    meta = EventMeta(
        event_id="p-1", correlation_id="c", user_id=uid, household_id="h", type="nudge.planned",
        occurred_at=NOW, trace_id="t",
    )  # fmt: skip
    w.deliver(meta, bad)
    assert w.store.items(uid) == [] and w.store.stats()["delivery_refused_template"] == 1


def test_hold_delivers_nothing() -> None:
    w = World('{"action":"hold","kind":null,"tone":"gentle","reason":"enough this week"}')
    uid = w.person(1)
    w.tick(uid)
    [(_, planned)] = w.published(NUDGES_PLANNED)
    assert planned.action == "hold" and planned.fallback_action == "send"  # a real disagreement
    w.deliver_planned()
    assert w.store.items(uid) == [] and w.store.stats()["disagree"] == 1
