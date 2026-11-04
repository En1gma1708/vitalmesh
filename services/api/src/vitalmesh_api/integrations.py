"""Routes for connecting Google Calendar and scheduling workouts (D80).

Safety properties (each has a test):
* the sign-in to Google is bound to the person who started it: the ``state`` value is random,
  single-use, expires in 10 minutes, and is looked up under the signed-in person's id, so a
  callback carrying someone else's state, a reused one or a made-up one connects nothing;
* nothing is written to the calendar without a separate confirmation of a proposal the person was
  shown; a proposal is kept 15 minutes, readable only by its owner, and works once;
* a person with a declared health condition gets a notice instead of a schedule;
* disconnecting revokes the permission at Google (best effort) and deletes the stored token;
  deleting the account does the same first.
"""

from __future__ import annotations

import contextlib
import logging
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from vitalmesh_stores import PendingImports

from vitalmesh_api.googlecal import (
    PROVIDER,
    Disconnected,
    GoogleClient,
    GoogleError,
    TokenBox,
    event_id,
    pkce_pair,
)
from vitalmesh_api.scheduler import SESSION_MINUTES, Slot, propose, refusal_for
from vitalmesh_api.security import Auth

log = logging.getLogger("vitalmesh.integrations")
OPAQUE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
EVENT_TITLE = "Workout"
EVENT_NOTE = "Planned with VitalMesh. General wellness, not medical advice."


class ProposeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minutes: int
    timezone: str


def install_integrations(
    app: FastAPI,
    auth: Auth,
    google: GoogleClient | None,
    box: TokenBox,
    pending: PendingImports,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    def uid_of(request: Request) -> str:
        return str(request.state.claims.user_id)

    def access_for(user_id: str) -> str:
        """A fresh access token for the person, or a clear error to connect again."""
        if google is None:
            raise HTTPException(status_code=503, detail="Google is not set up on this server.")
        conn = auth.store.get_connection(user_id, PROVIDER)
        if conn is None:
            raise HTTPException(status_code=409, detail="Connect Google Calendar first.")
        try:
            return google.access_token(box.open(conn.token_enc))
        except Disconnected as exc:
            auth.store.delete_connection(user_id, PROVIDER)  # it no longer works: say so once
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except GoogleError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    def disconnect(user_id: str) -> None:
        conn = auth.store.get_connection(user_id, PROVIDER)
        if conn is None:
            return
        if google is not None:
            with contextlib.suppress(GoogleError):  # unreadable token: nothing to revoke
                google.revoke(box.open(conn.token_enc))
        auth.store.delete_connection(user_id, PROVIDER)

    # the account-deletion hook: revoke before the rows go
    auth.before_delete = disconnect

    @app.get("/v1/me/integrations")
    def status(request: Request) -> dict[str, Any]:
        conn = auth.store.get_connection(uid_of(request), PROVIDER)
        return {
            "google": {
                "available": google is not None,
                "connected": conn is not None,
                "connected_at": conn.connected_at.isoformat() if conn else None,
            }
        }

    @app.post("/v1/me/integrations/google/start")
    def start(request: Request) -> dict[str, str]:
        user_id = uid_of(request)
        if google is None:
            raise HTTPException(status_code=503, detail="Google is not set up on this server.")
        if not auth.limiter.hit(f"google-start:{user_id}", 10, 3600):
            raise HTTPException(status_code=429, detail="too many attempts; try again later")
        state = secrets.token_urlsafe(24)
        verifier, challenge = pkce_pair()
        pending.put(user_id, state, {"purpose": "google", "verifier": verifier})
        auth.audit(request, user_id, "google_connect_started")
        return {"url": google.authorize_url(state, challenge)}

    @app.get("/v1/integrations/google/callback")
    def callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        user_id = uid_of(request)
        failed = RedirectResponse("/app#google=failed", status_code=303)
        saved = None
        if state and OPAQUE_ID.match(state):
            saved = pending.get(user_id, state)  # only a state issued to THIS session's person
            pending.delete(user_id, state)  # single use, even when the rest fails
        if saved is None or saved.get("purpose") != "google" or google is None:
            auth.audit(request, user_id, "google_connect_refused")
            return failed
        if error or not code or len(code) > 2048:
            auth.audit(request, user_id, "google_connect_declined")
            return failed
        try:
            refresh, scope = google.exchange(code, str(saved["verifier"]))
        except GoogleError:
            auth.audit(request, user_id, "google_connect_failed")
            return failed
        auth.store.put_connection(user_id, PROVIDER, box.seal(refresh), scope)
        auth.audit(request, user_id, "google_connected")
        return RedirectResponse("/app#google=connected", status_code=303)

    @app.delete("/v1/me/integrations/google")
    def remove(request: Request) -> dict[str, str]:
        user_id = uid_of(request)
        disconnect(user_id)
        auth.audit(request, user_id, "google_disconnected")
        return {"status": "disconnected"}

    @app.post("/v1/me/schedule/propose", status_code=201)
    def propose_route(body: ProposeIn, request: Request) -> dict[str, Any]:
        user_id = uid_of(request)
        if body.minutes not in SESSION_MINUTES:
            raise HTTPException(status_code=422, detail=f"minutes must be one of {SESSION_MINUTES}")
        try:
            tz = ZoneInfo(body.timezone)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise HTTPException(status_code=422, detail="unknown time zone") from None
        profile = auth.store.get_profile(user_id) or {}
        notice = refusal_for(list(profile.get("conditions", [])))
        if notice:
            raise HTTPException(status_code=409, detail=notice)
        if not auth.limiter.hit(f"schedule:{user_id}", 10, 3600):
            raise HTTPException(status_code=429, detail="too many requests; try again later")
        access = access_for(user_id)
        now = clock()
        try:
            busy = google.busy(access, now, now + timedelta(days=9)) if google else []
        except Disconnected as exc:
            auth.store.delete_connection(user_id, PROVIDER)
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except GoogleError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        plan = propose(busy, tz, now, body.minutes)
        if not plan.slots:
            raise HTTPException(
                status_code=409,
                detail="I found no free time in the next week; your calendar is full.",
            )
        proposal_id = secrets.token_urlsafe(12)
        pending.put(
            user_id,
            proposal_id,
            {
                "purpose": "schedule",
                "timezone": body.timezone,
                "minutes": plan.minutes,
                "slots": [[s.start.isoformat(), s.end.isoformat()] for s in plan.slots],
            },
        )
        return {
            "proposal_id": proposal_id,
            "minutes": plan.minutes,
            "wanted": plan.wanted,
            "short_by": plan.short_by,
            "total_minutes": plan.total_minutes,
            "sessions": [
                {"start": s.start.isoformat(), "end": s.end.isoformat()} for s in plan.slots
            ],
        }

    @app.post("/v1/me/schedule/{proposal_id}/confirm", status_code=202)
    def confirm(proposal_id: str, request: Request) -> dict[str, int]:
        user_id = uid_of(request)
        item = pending.get(user_id, proposal_id) if OPAQUE_ID.match(proposal_id) else None
        if item is None or item.get("purpose") != "schedule":
            raise HTTPException(status_code=404, detail="that proposal expired; ask again")
        access = access_for(user_id)
        created = already = 0
        try:
            for start_iso, end_iso in item["slots"]:
                slot = Slot(datetime.fromisoformat(start_iso), datetime.fromisoformat(end_iso))
                made = google.create_event(  # type: ignore[union-attr]
                    access, event_id(user_id, slot.start), EVENT_TITLE, EVENT_NOTE,
                    slot.start, slot.end, str(item["timezone"]),
                )  # fmt: skip
                created, already = created + int(made), already + int(not made)
        except Disconnected as exc:
            auth.store.delete_connection(user_id, PROVIDER)
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except GoogleError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        pending.delete(user_id, proposal_id)
        auth.audit(request, user_id, "calendar_events_created", created=created, already=already)
        return {"created": created, "already_there": already}

    @app.delete("/v1/me/schedule/{proposal_id}")
    def cancel(proposal_id: str, request: Request) -> dict[str, str]:
        if OPAQUE_ID.match(proposal_id):
            pending.delete(uid_of(request), proposal_id)
        return {"status": "cancelled"}
