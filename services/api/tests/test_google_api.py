"""Connecting Google Calendar and scheduling (D80), against a fake Google server."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from test_security import PROFILE, World, csrf
from vitalmesh_api.googlecal import (
    SCOPE,
    Disconnected,
    GoogleClient,
    GoogleConfig,
    GoogleError,
    TokenBox,
)

CFG = GoogleConfig("client-id-123", "client-secret-456", "http://127.0.0.1:8000/cb")
REFRESH = "refresh-token-SECRET-value"


class FakeGoogle:
    """Answers like Google's endpoints and records what it was asked."""

    def __init__(self) -> None:
        self.challenges: dict[str, str] = {}  # code -> challenge the browser flow used
        self.verifiers_ok: list[bool] = []
        self.revoked: list[str] = []
        self.inserted: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []
        self.refresh_invalid = False
        self.scope_back = SCOPE
        self.give_refresh = True
        self.calls: list[str] = []
        self.client = GoogleClient(CFG, httpx.Client(transport=httpx.MockTransport(self.handle)))

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(request.method + " " + url.split("?")[0])
        if url.startswith("https://oauth2.googleapis.com/token"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form["grant_type"] == "authorization_code":
                digest = hashlib.sha256(form["code_verifier"].encode()).digest()
                challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
                self.verifiers_ok.append(challenge == self.challenges.get(form["code"]))
                body: dict[str, Any] = {"access_token": "a", "scope": self.scope_back}
                if self.give_refresh:
                    body["refresh_token"] = REFRESH
                return httpx.Response(200, json=body)
            if self.refresh_invalid:
                return httpx.Response(400, json={"error": "invalid_grant"})
            assert form["refresh_token"] == REFRESH
            return httpx.Response(200, json={"access_token": "access-1"})
        if url.startswith("https://oauth2.googleapis.com/revoke"):
            self.revoked.append(request.content.decode())
            return httpx.Response(200)
        if request.method == "GET":
            assert request.headers["authorization"] == "Bearer access-1"
            return httpx.Response(200, json={"items": self.items})
        body = json.loads(request.content)
        if any(e["id"] == body["id"] for e in self.inserted):
            return httpx.Response(409, json={"error": "duplicate"})
        self.inserted.append(body)
        return httpx.Response(200, json={"id": body["id"]})


@pytest.fixture
def g() -> FakeGoogle:
    return FakeGoogle()


def connect(w: World, c: TestClient, g: FakeGoogle, code: str = "good-code") -> str:
    r = c.post("/v1/me/integrations/google/start", headers=csrf(c))
    assert r.status_code == 200, r.text
    q = parse_qs(urlparse(r.json()["url"]).query)
    g.challenges[code] = q["code_challenge"][0]
    return str(q["state"][0])


def callback(c: TestClient, state: str, code: str = "good-code", **extra: str) -> Any:
    params = {"code": code, "state": state, **extra}
    return c.get("/v1/integrations/google/callback", params=params, follow_redirects=False)


def ready(g: FakeGoogle) -> tuple[World, TestClient, str]:
    w = World(google=g.client)
    c = w.client()
    uid = w.signup(c)
    assert c.put("/v1/me/profile", json=PROFILE, headers=csrf(c)).status_code == 200
    return w, c, uid


# ---------------------------------------------------------------------------- connecting
def test_the_authorization_address_asks_for_one_scope_pkce_and_offline_access(
    g: FakeGoogle,
) -> None:
    _, c, _ = ready(g)
    url = c.post("/v1/me/integrations/google/start", headers=csrf(c)).json()["url"]
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert urlparse(url).netloc == "accounts.google.com"
    assert q["scope"] == SCOPE and q["code_challenge_method"] == "S256"
    assert q["access_type"] == "offline" and q["response_type"] == "code"
    assert q["client_id"] == CFG.client_id and "client_secret" not in q
    assert "login_hint" not in q  # the person's email is not sent along


def test_connecting_stores_the_refresh_token_encrypted_and_proves_the_verifier(
    g: FakeGoogle,
) -> None:
    w, c, uid = ready(g)
    state = connect(w, c, g)
    r = callback(c, state)
    assert r.status_code == 303 and r.headers["location"] == "/app#google=connected"
    assert g.verifiers_ok == [True]  # Google would check the PKCE verifier against the challenge
    stored = w.store.get_connection(uid, "google")
    assert stored is not None and REFRESH not in stored.token_enc and stored.scope == SCOPE
    assert c.get("/v1/me/integrations").json()["google"]["connected"] is True
    assert "google_connected" in [e["action"] for e in w.store.audit_entries(uid)]
    assert all(REFRESH not in str(e) for e in w.store.audit_entries(uid))


def test_a_state_works_once(g: FakeGoogle) -> None:
    w, c, uid = ready(g)
    state = connect(w, c, g)
    assert callback(c, state).headers["location"] == "/app#google=connected"
    w.store.delete_connection(uid, "google")
    again = callback(c, state)
    assert again.headers["location"] == "/app#google=failed"
    assert w.store.get_connection(uid, "google") is None


def test_someone_elses_or_a_made_up_state_connects_nothing(g: FakeGoogle) -> None:
    w, mine, my_id = ready(g)
    other = w.client()
    other_id = w.signup(other, "b@example.com")
    state = connect(w, mine, g)
    stolen = callback(other, state)  # the other person's session with MY state
    assert stolen.headers["location"] == "/app#google=failed"
    assert w.store.get_connection(other_id, "google") is None
    assert callback(mine, "not-a-real-state-value-123456").headers["location"].endswith("failed")
    assert callback(mine, "").headers["location"].endswith("failed")
    assert w.store.get_connection(my_id, "google") is None
    assert g.verifiers_ok == []  # Google was never even asked


def test_declining_or_a_missing_code_connects_nothing(g: FakeGoogle) -> None:
    w, c, uid = ready(g)
    state = connect(w, c, g)
    assert c.get(
        "/v1/integrations/google/callback", params={"error": "access_denied", "state": state},
        follow_redirects=False,
    ).headers["location"].endswith("failed")  # fmt: skip
    assert w.store.get_connection(uid, "google") is None


def test_a_grant_without_the_calendar_scope_or_a_refresh_token_is_refused(g: FakeGoogle) -> None:
    w, c, uid = ready(g)
    g.scope_back = "openid"
    assert callback(c, connect(w, c, g)).headers["location"].endswith("failed")
    g.scope_back, g.give_refresh = SCOPE, False
    assert callback(c, connect(w, c, g)).headers["location"].endswith("failed")
    assert w.store.get_connection(uid, "google") is None


def test_the_callback_needs_a_session(g: FakeGoogle) -> None:
    w, _, _ = ready(g)
    assert callback(w.client(), "x" * 20).status_code == 401


def test_starting_needs_the_csrf_header_and_a_configured_server() -> None:
    w = World()  # no Google client configured
    c = w.client()
    w.signup(c)
    assert c.get("/v1/me/integrations").json()["google"]["available"] is False
    assert c.post("/v1/me/integrations/google/start", headers=csrf(c)).status_code == 503
    g = FakeGoogle()
    _, c2, _ = ready(g)
    assert c2.post("/v1/me/integrations/google/start").status_code == 403


def test_disconnecting_revokes_at_google_and_deletes_the_token(g: FakeGoogle) -> None:
    w, c, uid = ready(g)
    callback(c, connect(w, c, g))
    assert c.delete("/v1/me/integrations/google", headers=csrf(c)).status_code == 200
    assert g.revoked == [f"token={REFRESH}"]
    assert w.store.get_connection(uid, "google") is None


def test_deleting_the_account_revokes_the_permission_first(g: FakeGoogle) -> None:
    w, c, _ = ready(g)
    callback(c, connect(w, c, g))
    r = c.request("DELETE", "/v1/me", json={"password": "correct horse battery"}, headers=csrf(c))
    assert r.status_code == 200 and len(g.revoked) == 1


# ------------------------------------------------------------------------- scheduling
def propose(c: TestClient, minutes: int = 45, tz: str = "Asia/Kolkata") -> Any:
    return c.post(
        "/v1/me/schedule/propose", json={"minutes": minutes, "timezone": tz}, headers=csrf(c)
    )


def test_proposing_needs_a_connection(g: FakeGoogle) -> None:
    _, c, _ = ready(g)
    r = propose(c)
    assert r.status_code == 409 and "Connect Google Calendar" in r.json()["detail"]


def test_a_proposal_writes_nothing_until_confirmed_and_confirming_works_once(
    g: FakeGoogle,
) -> None:
    w, c, uid = ready(g)
    callback(c, connect(w, c, g))
    r = propose(c, 45)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["wanted"] == 4 and len(plan["sessions"]) == 4 and plan["short_by"] == 0
    assert g.inserted == []  # nothing is on the calendar yet
    ok = c.post(f"/v1/me/schedule/{plan['proposal_id']}/confirm", headers=csrf(c))
    assert ok.status_code == 202 and ok.json() == {"created": 4, "already_there": 0}
    assert len(g.inserted) == 4
    first = g.inserted[0]
    assert first["summary"] == "Workout" and first["extendedProperties"]["private"] == {
        "vitalmesh": "1"
    }
    assert "not medical advice" in first["description"]
    assert (
        c.post(f"/v1/me/schedule/{plan['proposal_id']}/confirm", headers=csrf(c)).status_code == 404
    )
    assert "calendar_events_created" in [e["action"] for e in w.store.audit_entries(uid)]


def test_confirming_the_same_times_again_does_not_duplicate_events(g: FakeGoogle) -> None:
    w, c, _ = ready(g)
    callback(c, connect(w, c, g))
    for expected in ({"created": 3, "already_there": 0}, {"created": 0, "already_there": 3}):
        plan = propose(c, 60).json()
        out = c.post(f"/v1/me/schedule/{plan['proposal_id']}/confirm", headers=csrf(c)).json()
        assert out == expected
    assert len(g.inserted) == 3


def test_existing_events_are_avoided_and_all_day_or_free_events_are_not(g: FakeGoogle) -> None:
    w, c, _ = ready(g)
    callback(c, connect(w, c, g))
    tomorrow = (w.now + timedelta(days=1)).date().isoformat()

    def timed(start: str, end: str, **extra: str) -> dict[str, Any]:
        return {
            "start": {"dateTime": f"{tomorrow}T{start}:00+05:30"},
            "end": {"dateTime": f"{tomorrow}T{end}:00+05:30"},
            **extra,
        }

    g.items = [
        timed("12:00", "22:00"),
        {"start": {"date": tomorrow}, "end": {"date": tomorrow}, "status": "confirmed"},
        timed("09:00", "10:00", transparency="transparent"),
    ]
    plan = propose(c, 30).json()
    on_tomorrow = [s for s in plan["sessions"] if s["start"].startswith(tomorrow)]
    for s in on_tomorrow:  # the 12:00-22:00 event, plus 15 minutes either side, is off limits
        hhmm = s["start"][11:16]
        assert hhmm <= "11:15" or hhmm >= "22:15", s


def test_a_person_with_a_declared_condition_gets_a_notice_and_google_is_not_asked(
    g: FakeGoogle,
) -> None:
    w = World(google=g.client)
    c = w.client()
    w.signup(c)
    profile = {**PROFILE, "conditions": ["cardiac"]}
    assert c.put("/v1/me/profile", json=profile, headers=csrf(c)).status_code == 200
    callback(c, connect(w, c, g))
    calls_before = len(g.calls)
    r = propose(c)
    assert r.status_code == 409 and "clinician" in r.json()["detail"]
    assert len(g.calls) == calls_before


def test_bad_session_lengths_and_time_zones_are_refused(g: FakeGoogle) -> None:
    w, c, _ = ready(g)
    callback(c, connect(w, c, g))
    assert propose(c, 25).status_code == 422
    assert propose(c, 30, "Mars/Olympus").status_code == 422
    assert c.post(
        "/v1/me/schedule/propose", json={"minutes": 30, "timezone": "UTC", "x": 1}, headers=csrf(c)
    ).status_code == 422  # fmt: skip


def test_another_person_cannot_confirm_my_proposal(g: FakeGoogle) -> None:
    w, mine, _ = ready(g)
    callback(mine, connect(w, mine, g))
    plan = propose(mine, 60).json()
    theirs = w.client()
    w.signup(theirs, "b@example.com")
    r = theirs.post(f"/v1/me/schedule/{plan['proposal_id']}/confirm", headers=csrf(theirs))
    assert r.status_code == 404 and g.inserted == []


def test_a_permission_that_stopped_working_is_cleared_and_the_person_is_told(
    g: FakeGoogle,
) -> None:
    w, c, uid = ready(g)
    callback(c, connect(w, c, g))
    g.refresh_invalid = True  # revoked at Google, or the 7-day limit of a test app
    r = propose(c)
    assert r.status_code == 409 and "connect Google Calendar again" in r.json()["detail"]
    assert w.store.get_connection(uid, "google") is None


def test_scheduling_calls_are_limited_per_person(g: FakeGoogle) -> None:
    w, c, _ = ready(g)
    callback(c, connect(w, c, g))
    codes = [propose(c, 60).status_code for _ in range(11)]
    assert codes[:10] == [201] * 10 and codes[10] == 429


def test_scheduling_needs_the_csrf_header(g: FakeGoogle) -> None:
    w, c, _ = ready(g)
    callback(c, connect(w, c, g))
    r = c.post("/v1/me/schedule/propose", json={"minutes": 30, "timezone": "UTC"})
    assert r.status_code == 403
    assert c.post("/v1/me/schedule/abcdefgh1234/confirm").status_code == 403


# --------------------------------------------------------------------- client details
def test_the_token_box_round_trips_and_a_different_key_cannot_read_it() -> None:
    from cryptography.fernet import Fernet

    a, b = TokenBox(Fernet.generate_key()), TokenBox(Fernet.generate_key())
    sealed = a.seal("secret-token")
    assert "secret-token" not in sealed and a.open(sealed) == "secret-token"
    with pytest.raises(GoogleError, match="connect Google Calendar again"):
        b.open(sealed)


def test_errors_shown_to_people_never_contain_tokens_or_secrets(g: FakeGoogle) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "server", "echo": CFG.client_secret})

    client = GoogleClient(CFG, httpx.Client(transport=httpx.MockTransport(broken)))
    with pytest.raises(GoogleError) as info:
        client.access_token(REFRESH)
    assert REFRESH not in str(info.value) and CFG.client_secret not in str(info.value)
    revoked = FakeGoogle()
    revoked.refresh_invalid = True
    with pytest.raises(Disconnected):
        revoked.client.access_token(REFRESH)
