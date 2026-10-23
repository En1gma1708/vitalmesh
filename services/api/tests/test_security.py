"""The security rules of D75, each as a test an attacker's request would fail."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from vitalmesh_accounts import (
    MAX_FAILURES,
    Claims,
    InMemoryAccountStore,
    InMemoryRateLimiter,
    TokenIssuer,
    hash_password,
)
from vitalmesh_api.app import create_app
from vitalmesh_api.onboarding import CONSENT_VERSION
from vitalmesh_api.security import CSRF_COOKIE, Auth
from vitalmesh_common.testing import PlainSerde, RecordingPublisher
from vitalmesh_stores import InMemoryRunLog
from vitalmesh_t0.profiles import InMemoryProfileStore

SECRET = b"s" * 32
PASSWORD = "correct horse battery"
PROFILE: dict[str, Any] = {
    "sex": "female",
    "age_years": 34,
    "weight_kg": 68,
    "height_cm": 168,
    "activity": "moderate",
    "resting_hr_bpm": 62,
    "goal": "maintain",
    "diet": "vegan",
    "allergens": ["egg"],
    "conditions": [],
    "medications": [],
    "consent": True,
    "consent_version": CONSENT_VERSION,
}


class World:
    def __init__(self) -> None:
        self.store = InMemoryAccountStore()
        self.cache = InMemoryProfileStore()
        self.limiter = InMemoryRateLimiter()
        self.purged: list[str] = []
        self.runs = InMemoryRunLog()
        self.publisher = RecordingPublisher()
        self.auth = Auth(
            self.store,
            TokenIssuer(SECRET),
            self.limiter,
            self.cache,
            purge=self.purged.append,
            export_extra=lambda uid: {"measurements": 0},
        )
        self.app = create_app(
            self.cache, PlainSerde(), self.publisher, auth=self.auth, runs=self.runs,
            ops=lambda: {"tiers": {}, "cohort": {}},
        )  # fmt: skip

    def client(self) -> TestClient:
        return TestClient(self.app)

    def signup(self, c: TestClient, email: str = "a@example.com") -> str:
        r = c.post("/v1/auth/signup", json={"email": email, "password": PASSWORD})
        assert r.status_code == 201, r.text
        return str(r.json()["user_id"])


def csrf(c: TestClient) -> dict[str, str]:
    return {"x-csrf-token": c.cookies.get(CSRF_COOKIE) or ""}


@pytest.fixture
def w() -> World:
    return World()


# ------------------------------------------------------------------------ deny by default
def test_every_v1_route_needs_a_session_except_the_public_ones(w: World) -> None:
    c = w.client()
    assert c.get("/healthz").status_code == 200
    assert c.get("/v1/auth/choices").status_code == 200
    for path in (
        "/v1/me",
        "/v1/users/u-1/recommendation",
        "/v1/users/u-1/series/body_weight",
        "/v1/ops/tiers",
        "/v1/something/nobody/wrote/a/rule/for",
    ):
        assert c.get(path).status_code == 401, path
    for path in ("/v1/measurements", "/v1/labs", "/v1/users/u-1/questions"):
        assert c.post(path, json={}).status_code == 401, path


def test_a_route_with_no_rule_is_refused_even_when_signed_in(w: World) -> None:
    c = w.client()
    w.signup(c)
    assert c.get("/v1/something/nobody/wrote/a/rule/for").status_code == 404


# --------------------------------------------------------------------------- sign-up
def test_signup_sets_hardened_cookies_and_never_returns_the_hash(w: World) -> None:
    c = w.client()
    r = c.post("/v1/auth/signup", json={"email": "A@Example.com ", "password": PASSWORD})
    assert r.status_code == 201 and r.json()["needs_onboarding"] is True
    cookies = r.headers.get_list("set-cookie")
    access = next(x for x in cookies if x.startswith("vm_access="))
    refresh = next(x for x in cookies if x.startswith("vm_refresh="))
    csrf_cookie = next(x for x in cookies if x.startswith("vm_csrf="))
    assert "HttpOnly" in access and "samesite=lax" in access.lower()
    assert "HttpOnly" in refresh and "samesite=strict" in refresh.lower() and "/v1/auth" in refresh
    assert "HttpOnly" not in csrf_cookie  # the page must read it
    assert "argon2" not in r.text and PASSWORD not in r.text
    assert w.store.by_email("a@example.com") is not None  # normalised


@pytest.mark.parametrize(
    ("email", "password", "fragment"),
    [
        ("not-an-email", PASSWORD, "valid email"),
        ("a@example.com", "short", "at least"),
        ("a@example.com", "password123", "common"),
    ],
)
def test_weak_or_malformed_signups_are_refused_with_a_reason(
    w: World, email: str, password: str, fragment: str
) -> None:
    r = w.client().post("/v1/auth/signup", json={"email": email, "password": password})
    assert r.status_code == 422 and fragment in r.json()["detail"]


def test_a_second_signup_with_the_same_email_is_refused(w: World) -> None:
    w.signup(w.client())
    r = w.client().post("/v1/auth/signup", json={"email": "a@example.com", "password": PASSWORD})
    assert r.status_code == 409


def test_signups_from_one_address_are_rate_limited(w: World) -> None:
    c = w.client()
    codes = [
        c.post("/v1/auth/signup", json={"email": f"u{i}@example.com", "password": PASSWORD})
        for i in range(12)
    ]
    assert [r.status_code for r in codes][:10] == [201] * 10 and codes[-1].status_code == 429


# ----------------------------------------------------------------------------- login
def test_login_failures_look_the_same_for_unknown_emails_and_wrong_passwords(w: World) -> None:
    w.signup(w.client())
    c = w.client()
    wrong = c.post("/v1/auth/login", json={"email": "a@example.com", "password": "nope-nope-nope"})
    unknown = c.post(
        "/v1/auth/login", json={"email": "x@example.com", "password": "nope-nope-nope"}
    )
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json()


def test_an_account_locks_after_repeated_failures_even_for_the_right_password(w: World) -> None:
    uid = w.signup(w.client())
    c = w.client()
    for _ in range(MAX_FAILURES):
        c.post("/v1/auth/login", json={"email": "a@example.com", "password": "wrong-wrong-wrong"})
    ok = c.post("/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    assert ok.status_code == 429
    assert any(e["action"] == "login_locked" for e in w.store.audit_entries(uid))


def test_login_works_and_clears_the_failure_count(w: World) -> None:
    uid = w.signup(w.client())
    c = w.client()
    c.post("/v1/auth/login", json={"email": "a@example.com", "password": "wrong-wrong-wrong"})
    r = c.post("/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["user_id"] == uid
    user = w.store.by_id(uid)
    assert user is not None and user.failed_logins == 0


# ---------------------------------------------------------------- authorization and CSRF
def test_a_person_cannot_read_or_change_another_persons_data(w: World) -> None:
    a, b = w.client(), w.client()
    uid_a, uid_b = w.signup(a, "a@example.com"), w.signup(b, "b@example.com")
    assert a.get(f"/v1/users/{uid_b}/recommendation").status_code == 404  # not even confirmed
    assert a.get(f"/v1/users/{uid_b}/runs/x").status_code == 404
    body = {"user_id": uid_b, "metric_type": "body_weight", "value": 70, "unit": "kg",
            "measured_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}  # fmt: skip
    assert a.post("/v1/measurements", json=body, headers=csrf(a)).status_code == 403
    assert w.publisher.sent == []
    own = {**body, "user_id": uid_a}
    assert a.post("/v1/measurements", json=own, headers=csrf(a)).status_code == 202


def test_state_changing_requests_need_the_csrf_token(w: World) -> None:
    c = w.client()
    uid = w.signup(c)
    body = {"user_id": uid, "metric_type": "body_weight", "value": 70, "unit": "kg",
            "measured_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}  # fmt: skip
    assert c.post("/v1/measurements", json=body).status_code == 403
    assert (
        c.post("/v1/measurements", json=body, headers={"x-csrf-token": "wrong"}).status_code == 403
    )
    assert c.post("/v1/measurements", json=body, headers=csrf(c)).status_code == 202


def test_operator_routes_are_for_operators_only(w: World) -> None:
    user_client = w.client()
    w.signup(user_client)
    assert user_client.get("/v1/ops/tiers").status_code == 403
    w.store.create_user("ops@example.com", hash_password(PASSWORD), role="operator")
    op = w.client()
    assert (
        op.post(
            "/v1/auth/login", json={"email": "ops@example.com", "password": PASSWORD}
        ).status_code
        == 200
    )
    assert op.get("/v1/ops/tiers").status_code == 200
    # an operator sees internals, not another person's health data
    assert op.get("/v1/users/u-someone/series/body_weight").status_code == 404


def test_a_bearer_token_works_without_cookies_and_without_csrf(w: World) -> None:
    c = w.client()
    uid = w.signup(c)
    session = next(iter(w.store._sessions.values()))
    token = TokenIssuer(SECRET).access(Claims(uid, "user", session.session_id))
    fresh = w.client()
    assert fresh.get("/v1/me", headers={"authorization": f"Bearer {token}"}).status_code == 200
    assert fresh.get("/v1/me", headers={"authorization": "Bearer not.a.token"}).status_code == 401


# --------------------------------------------------------------- sessions: refresh, logout
def test_logout_revokes_the_session_server_side_at_once(w: World) -> None:
    c = w.client()
    w.signup(c)
    stolen = c.cookies.get("vm_access")
    assert c.post("/v1/auth/logout", headers=csrf(c)).status_code == 200
    thief = w.client()
    assert thief.get("/v1/me", headers={"authorization": f"Bearer {stolen}"}).status_code == 401


def test_refresh_rotates_and_replaying_the_old_token_ends_the_session(w: World) -> None:
    c = w.client()
    w.signup(c)
    old_refresh = c.cookies.get("vm_refresh")
    assert c.post("/v1/auth/refresh").status_code == 200  # rotated
    new_access = c.cookies.get("vm_access")
    thief = w.client()
    thief.cookies.set("vm_refresh", old_refresh or "", path="/v1/auth")
    assert thief.post("/v1/auth/refresh").status_code == 401  # reuse detected
    # ... and the legitimate session is revoked too: the stolen copy forced a new login
    assert (
        w.client().get("/v1/me", headers={"authorization": f"Bearer {new_access}"}).status_code
        == 401
    )


def test_refresh_without_a_cookie_is_refused(w: World) -> None:
    assert w.client().post("/v1/auth/refresh").status_code == 401


# ------------------------------------------------------------------- onboarding profile
def test_onboarding_saves_durably_and_fills_the_agents_cache(w: World) -> None:
    c = w.client()
    uid = w.signup(c)
    assert c.get("/v1/me").json()["has_profile"] is False
    r = c.put("/v1/me/profile", json=PROFILE, headers=csrf(c))
    assert r.status_code == 200
    me = c.get("/v1/me").json()
    assert me["has_profile"] is True and me["consent_version"] == CONSENT_VERSION
    record = w.cache.get(uid)
    assert record is not None and record.profile.diet == "vegan"
    assert "egg" in record.profile.allergens and record.profile.user_id == uid
    assert w.store.get_profile(uid)["allergens"] == ["egg"]  # type: ignore[index]


@pytest.mark.parametrize(
    "change",
    [
        {"allergens": ["pollen"]},
        {"conditions": ["made_up_condition"]},
        {"medications": ["ibuprofen_400mg"]},
        {"diet": "carnivore"},
        {"age_years": 12},
        {"weight_kg": 900},
        {"consent": False},
        {"consent_version": "old"},
        {"extra_field": 1},
    ],
)
def test_onboarding_rejects_anything_outside_the_closed_lists_and_ranges(
    w: World, change: dict[str, Any]
) -> None:
    c = w.client()
    uid = w.signup(c)
    r = c.put("/v1/me/profile", json={**PROFILE, **change}, headers=csrf(c))
    assert r.status_code == 422 and w.cache.get(uid) is None


def test_the_users_id_in_the_profile_is_always_the_sessions_id(w: World) -> None:
    c = w.client()
    uid = w.signup(c)
    c.put("/v1/me/profile", json={**PROFILE, "user_id": "u-someone-else"}, headers=csrf(c))
    assert w.cache.get("u-someone-else") is None  # extra fields are refused, never trusted
    assert w.cache.get(uid) is None


# ----------------------------------------------------------- privacy: export and delete
def test_export_has_the_persons_data_and_no_secrets(w: World) -> None:
    c = w.client()
    w.signup(c)
    c.put("/v1/me/profile", json=PROFILE, headers=csrf(c))
    data = c.get("/v1/me/export").json()
    assert data["profile"]["diet"] == "vegan" and data["account"]["email"] == "a@example.com"
    assert "pw_hash" not in str(data) and "argon2" not in str(data)
    assert any(e["action"] == "profile_saved" for e in data["audit"])


def test_deleting_the_account_needs_the_password_and_removes_everything(w: World) -> None:
    c = w.client()
    uid = w.signup(c)
    c.put("/v1/me/profile", json=PROFILE, headers=csrf(c))
    assert (
        c.request(
            "DELETE", "/v1/me", json={"password": "wrong-wrong-wrong"}, headers=csrf(c)
        ).status_code
        == 403
    )
    assert w.store.by_id(uid) is not None
    assert (
        c.request("DELETE", "/v1/me", json={"password": PASSWORD}, headers=csrf(c)).status_code
        == 200
    )
    assert w.store.by_id(uid) is None and w.purged == [uid]
    again = w.client().post("/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    assert again.status_code == 401


# --------------------------------------------------------------------- guardrails and headers
def test_asking_questions_is_rate_limited_per_person(w: World) -> None:
    c = w.client()
    uid = w.signup(c)
    c.put("/v1/me/profile", json=PROFILE, headers=csrf(c))
    codes = [
        c.post(
            f"/v1/users/{uid}/questions", json={"question": "What should I eat?"}, headers=csrf(c)
        ).status_code
        for _ in range(22)
    ]
    assert codes[:20] == [202] * 20 and codes[20:] == [429, 429]


def test_every_response_carries_the_security_headers(w: World) -> None:
    r = w.client().get("/healthz")
    csp = r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in csp and "default-src 'none'" in csp
    assert "unsafe-inline" not in csp.split("style-src-attr")[0]  # scripts and styles need a nonce
    other = w.client().get("/healthz").headers["content-security-policy"]
    assert csp != other  # a fresh nonce per response


def test_api_responses_are_not_cached(w: World) -> None:
    c = w.client()
    w.signup(c)
    assert c.get("/v1/me").headers["cache-control"] == "no-store"


def test_there_is_no_cors_so_other_sites_cannot_read_responses(w: World) -> None:
    r = w.client().get("/v1/auth/choices", headers={"origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


# ------------------------------------------------------------------------------------ pages
def test_pages_carry_the_response_nonce_that_the_policy_allows(w: World) -> None:
    c = w.client()
    for path in ("/", "/app"):
        r = c.get(path)
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        nonce = r.headers["content-security-policy"].split("'nonce-")[1].split("'")[0]
        assert f'<script nonce="{nonce}">' in r.text and f'<style nonce="{nonce}">' in r.text
        assert "<script>" not in r.text and "<style>" not in r.text


def test_the_old_id_in_the_url_form_redirects_to_the_session_based_page(w: World) -> None:
    r = w.client().get("/app/demo-1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app"


def test_the_pages_show_no_internals_to_a_person(w: World) -> None:
    c = w.client()
    for path in ("/", "/app"):
        page = c.get(path).text.lower()
        for internal in ("guardian", "tier", "kafka", "agent", "provenance", "rules_version"):
            assert internal not in page, (path, internal)


def test_the_welcome_page_offers_exactly_the_closed_lists(w: World) -> None:
    data = w.client().get("/v1/auth/choices").json()
    assert data["consent_version"] == CONSENT_VERSION
    assert set(data["choices"]) == {
        "sex", "activity", "goal", "diet", "allergens", "conditions", "medications",
    }  # fmt: skip
    assert "egg" in data["choices"]["allergens"] and "cardiac" in data["choices"]["conditions"]
