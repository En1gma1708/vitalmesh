"""Engagement and Delivery handlers (D87).

``engagement.tick`` -Engagement-> ``nudge.planned`` -Delivery-> inbox + ``notification.sent``

The Engagement handler is the agent's body: gather, filter by policy (code), decide (model or
cache or fallback), publish the decision. It publishes NOTHING when the policy blocks or there is
nothing to say; counters in the nudge store say why (``stats``), which is how a million ticks stay
cheap. The Delivery handler is code only and checks the hard policy once more before anything
reaches the inbox: the agent can ask, only Delivery can deliver.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_common.envelope import EventMeta, utcnow
from vitalmesh_common.processor import EventSerde, Handler, Publisher
from vitalmesh_common.topics import NOTIFICATIONS_SENT, NUDGES_PLANNED
from vitalmesh_stores import HistoryStore, Item, NudgeStore, ProfileStore

from vitalmesh_agents.engagement import (
    LOG_METRICS,
    Decision,
    Engagement,
    Option,
    Situation,
    candidates,
    dismissed_streak,
    fallback,
    global_block,
    kind_block,
    part_of_day,
    render,
)
from vitalmesh_agents.events import emit, next_meta

log = logging.getLogger("vitalmesh.engagement")
TICK = "engagement.tick"
PLANNED = "nudge.planned"
HISTORY_DAYS = 28


def make_engagement_handler(
    agent: Engagement,
    profiles: ProfileStore,
    history: HistoryStore,
    store: NudgeStore,
    serde: EventSerde,
    publisher: Publisher,
    clock: Callable[[], datetime] = utcnow,
) -> Handler:
    def handle(meta: EventMeta, _payload: Any) -> None:
        if meta.type != TICK:
            return
        user = meta.user_id
        prefs = store.get_prefs(user)
        now = clock()
        items = store.items(user)
        why = global_block(prefs, now, items)
        if why is not None:
            store.stat(f"blocked_{why}")  # no event, no model call
            return
        record = profiles.get(user)
        if record is None:
            store.stat("no_profile")
            return
        zone = ZoneInfo(prefs.timezone)
        since = now - timedelta(days=HISTORY_DAYS)
        series = {m: history.series(user, m, since) for m in (*LOG_METRICS,)}
        found = candidates(now, zone, sorted(record.profile.conditions), series)
        if not found:
            store.stat("no_candidates")
            return
        options: list[Option] = []
        blocked: list[str] = []
        for c in found:
            stopped = kind_block(c.kind, now, items)
            if stopped is not None:
                blocked.append(f"{c.kind}:{stopped}")
                store.stat(f"blocked_{stopped}")
                continue
            week = [i for i in items if i.kind == c.kind and now - i.at < timedelta(days=7)]
            options.append(Option(c.kind, c.params, dismissed_streak(c.kind, items), len(week)))
        if not options:
            return
        local = now.astimezone(zone)
        situation = Situation(
            tuple(options),
            part_of_day(local),
            sum(1 for i in items if i.at.astimezone(zone).date() == local.date()),
            sum(1 for i in items if now - i.at < timedelta(days=7)),
        )
        plain = fallback(situation)
        decision = _decide(agent, store, situation, meta.correlation_id)
        store.stat("agree" if decision.same_as(plain) else "disagree")
        store.stat(f"decided_{decision.decided_by}")
        store.stat(f"action_{decision.action}")

        planned = next_meta(meta, PLANNED, "nudge", now)
        chosen = next((o for o in options if o.kind == decision.kind), None)
        msg = payloads_pb2.NudgePlanned(
            nudge_id=planned.event_id,
            action=decision.action,
            kind=decision.kind or "",
            tone=decision.tone,
            decided_by=decision.decided_by,
            reason=decision.reason,
            fallback_action=plain.action,
            fallback_kind=plain.kind or "",
        )
        if chosen is not None:
            msg.params.update(chosen.params)
        msg.considered.extend(o.kind for o in options)
        msg.blocked.extend(blocked)
        emit(serde, publisher, NUDGES_PLANNED, planned, msg)
        publisher.flush()

    return handle


def _decide(agent: Engagement, store: NudgeStore, situation: Situation, session: str) -> Decision:
    key = situation.key()
    cached = store.decision(key)
    if cached is not None:
        action, _, rest = cached.partition("|")
        kind, _, tone = rest.partition("|")
        kinds = {o.kind for o in situation.options}
        if action in ("send", "hold") and (action == "hold" or kind in kinds):
            store.stat("cache_hit")
            return Decision(action, kind or None, tone or "gentle", "cached decision", "cache")  # type: ignore[arg-type]
    store.stat("cache_miss")
    decision = agent.decide(situation, session_id=session)
    store.stat("model_calls", decision.model_calls)
    store.stat("model_tokens", decision.prompt_tokens + decision.completion_tokens)
    if decision.decided_by == "agent":
        store.remember_decision(key, f"{decision.action}|{decision.kind or ''}|{decision.tone}")
    else:
        store.stat("fallback_used")
    return decision


def make_delivery_handler(
    store: NudgeStore,
    serde: EventSerde,
    publisher: Publisher,
    clock: Callable[[], datetime] = utcnow,
) -> Handler:
    def handle(meta: EventMeta, payload: Any) -> None:
        if meta.type != PLANNED:
            return
        if payload.action != "send":
            store.stat("delivery_hold")
            return
        user, now = meta.user_id, clock()
        items = store.items(user)
        # the hard policy again: the agent can ask, only this code can deliver
        veto = global_block(store.get_prefs(user), now, items) or kind_block(
            payload.kind, now, items
        )
        if veto is not None:
            store.stat(f"delivery_vetoed_{veto}")
            log.info("delivery vetoed %s for a nudge: %s", payload.kind, veto)
            return
        text = render(payload.kind, payload.tone, dict(payload.params))
        if text is None:
            store.stat("delivery_refused_template")
            log.warning(
                "delivery refused a nudge it could not render (%s/%s)", payload.kind, payload.tone
            )
            return
        if not store.add(user, Item(payload.nudge_id, payload.kind, payload.tone, text, now)):
            return  # redelivered event: already in the inbox
        store.stat("delivered")
        sent = next_meta(meta, "notification.sent", "delivered", now)
        note = payloads_pb2.NotificationSent(
            nudge_id=payload.nudge_id, kind=payload.kind, channel="in_app"
        )
        emit(serde, publisher, NOTIFICATIONS_SENT, sent, note)
        publisher.flush()

    return handle
