"""Accounts, sessions and the deny-by-default gate in front of the API (D75).

What this module guarantees, each with a test:
* Every ``/v1`` route needs a valid session except the few listed in ``PUBLIC``; a route nobody
  wrote a rule for is refused (deny by default), not left open.
* A person reaches only their own data: ``/v1/users/{id}/...`` requires that the id is the one in
  the session. Operators see the operator routes (tier mix, run internals), never a user's data.
* Cookie sessions are protected against cross-site requests: ``SameSite`` cookies plus a
  double-submit CSRF token on every state-changing request.
* Login is slowed (per address and per email), accounts lock after repeated failures, a refresh
  token is single-use and its reuse ends the session, logout revokes server-side at once.
* Every response carries the security headers; pages get a per-response script nonce.
* The audit log records who did what from where. It never records passwords, tokens or health
  data.
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from vitalmesh_accounts import (
    REFRESH_TTL_S,
    AccountStore,
    Claims,
    EmailTaken,
    RateLimiter,
    TokenError,
    TokenIssuer,
    check_email,
    check_password,
    hash_password,
    needs_rehash,
    new_refresh_token,
    normalise_email,
    verify_password,
)
from vitalmesh_accounts.tokens import hash_refresh
from vitalmesh_stores import ProfileStore

from vitalmesh_api.onboarding import CHOICES, CONSENT_VERSION, ProfileIn, stored_form

log = logging.getLogger("vitalmesh.security")

ACCESS_COOKIE, REFRESH_COOKIE, CSRF_COOKIE = "vm_access", "vm_refresh", "vm_csrf"
CSRF_HEADER = "x-csrf-token"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
PUBLIC = {
    "/healthz",
    "/v1/auth/signup",
    "/v1/auth/login",
    "/v1/auth/refresh",
    "/v1/auth/logout",
    "/v1/auth/choices",
}
_USER_PATH = re.compile(r"^/v1/users/([^/]+)(?:/|$)")
_ME_PATH = re.compile(r"^/v1/me(?:/|$)")
GENERIC_LOGIN_FAILURE = "That email and password do not match."


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=320)
    password: str = Field(max_length=256)


class DeleteAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(max_length=256)


class Auth:
    def __init__(
        self,
        store: AccountStore,
        issuer: TokenIssuer,
        limiter: RateLimiter,
        profile_cache: ProfileStore,
        *,
        purge: Callable[[str], None] = lambda _uid: None,
        export_extra: Callable[[str], dict[str, Any]] = lambda _uid: {},
        secure_cookies: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store, self.issuer, self.limiter = store, issuer, limiter
        self.profile_cache, self.purge, self.export_extra = profile_cache, purge, export_extra
        self.secure, self.clock = secure_cookies, clock

    # ------------------------------------------------------------------ session plumbing
    def authenticate(self, request: Request) -> Claims | None:
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else None
        token = token or request.cookies.get(ACCESS_COOKIE)
        if not token:
            return None
        try:
            claims = self.issuer.verify_access(token, self.clock())
        except TokenError:
            return None
        session = self.store.session(claims.session_id)
        if session is None or session.revoked or session.user_id != claims.user_id:
            return None  # logged out or revoked: effective at once, not when the token expires
        return claims

    def _start_session(self, user_id: str, role: str, response: Response) -> str:
        raw, hashed = new_refresh_token()
        now = self.clock()
        session = self.store.create_session(user_id, hashed, now + timedelta(seconds=REFRESH_TTL_S))
        self._cookies(response, user_id, role, session.session_id, raw)
        return session.session_id

    def _cookies(
        self, response: Response, user_id: str, role: str, session_id: str, refresh_raw: str
    ) -> None:
        access = self.issuer.access(Claims(user_id, role, session_id), self.clock())
        csrf = secrets.token_urlsafe(24)
        response.set_cookie(
            ACCESS_COOKIE, access, max_age=900, httponly=True, path="/",
            secure=self.secure, samesite="lax",
        )  # fmt: skip
        response.set_cookie(
            REFRESH_COOKIE, refresh_raw, max_age=REFRESH_TTL_S, httponly=True, path="/v1/auth",
            secure=self.secure, samesite="strict",
        )  # fmt: skip
        # readable on purpose: the page copies it into a header (double-submit CSRF token)
        response.set_cookie(
            CSRF_COOKIE, csrf, max_age=REFRESH_TTL_S, httponly=False, path="/",
            secure=self.secure, samesite="lax",
        )  # fmt: skip

    @staticmethod
    def clear_cookies(response: Response) -> None:
        response.delete_cookie(ACCESS_COOKIE, path="/")
        response.delete_cookie(REFRESH_COOKIE, path="/v1/auth")
        response.delete_cookie(CSRF_COOKIE, path="/")

    @staticmethod
    def ip_of(request: Request) -> str:
        # No proxy is trusted here: behind a load balancer, set the real client address upstream.
        return request.client.host if request.client else "unknown"

    def audit(self, request: Request, user_id: str | None, action: str, **detail: Any) -> None:
        self.store.audit(user_id, action, self.ip_of(request), detail)


def sse_safe_error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def install(app: FastAPI, auth: Auth) -> None:
    """Add the account routes and the gate."""

    # ---------------------------------------------------------------------- the gate
    @app.middleware("http")
    async def gate(request: Request, call_next: Callable[[Request], Any]) -> Response:
        request.state.nonce = secrets.token_urlsafe(16)
        path, method = request.url.path, request.method
        request.state.claims = None
        if path.startswith("/v1/") and path not in PUBLIC:
            claims = await run_in_threadpool(auth.authenticate, request)
            if claims is None:
                return _secured(request, sse_safe_error(401, "sign in to continue"), auth)
            request.state.claims = claims
            if method in UNSAFE and not _csrf_ok(request):
                return _secured(request, sse_safe_error(403, "missing or wrong CSRF token"), auth)
            denial = _authorise(path, claims)
            if denial is not None:
                return _secured(request, sse_safe_error(*denial), auth)
        response: Response = await call_next(request)
        return _secured(request, response, auth)

    # --------------------------------------------------------------------- public routes
    @app.get("/v1/auth/choices")
    def choices() -> dict[str, Any]:
        return {"choices": CHOICES, "consent_version": CONSENT_VERSION}

    @app.post("/v1/auth/signup", status_code=201)
    def signup(body: Credentials, request: Request, response: Response) -> dict[str, Any]:
        ip = auth.ip_of(request)
        if not auth.limiter.hit(f"signup:{ip}", 10, 3600):
            raise HTTPException(status_code=429, detail="too many sign-ups from here; try later")
        email = normalise_email(body.email)
        if (problem := check_email(email)) is not None:
            raise HTTPException(status_code=422, detail=problem)
        if problems := check_password(body.password, email):
            raise HTTPException(status_code=422, detail="; ".join(problems))
        try:
            user = auth.store.create_user(email, hash_password(body.password))
        except EmailTaken:
            raise HTTPException(
                status_code=409, detail="that email is already registered"
            ) from None
        auth._start_session(user.user_id, user.role, response)
        auth.audit(request, user.user_id, "signup")
        return {"user_id": user.user_id, "needs_onboarding": True}

    @app.post("/v1/auth/login")
    def login(body: Credentials, request: Request, response: Response) -> dict[str, Any]:
        ip, email = auth.ip_of(request), normalise_email(body.email)
        if not auth.limiter.hit(f"login-ip:{ip}", 30, 600) or not auth.limiter.hit(
            f"login-email:{email}", 10, 600
        ):
            raise HTTPException(status_code=429, detail="too many attempts; try again later")
        user = auth.store.by_email(email)
        now = auth.clock()
        if user and user.locked_until and user.locked_until > now:
            auth.audit(request, user.user_id, "login_locked")
            raise HTTPException(status_code=429, detail="too many attempts; try again later")
        if not verify_password(user.pw_hash if user else None, body.password) or user is None:
            if user:
                auth.store.record_failure(user.user_id, now)
                auth.audit(request, user.user_id, "login_failed")
            raise HTTPException(status_code=401, detail=GENERIC_LOGIN_FAILURE)
        auth.store.clear_failures(user.user_id)
        if needs_rehash(user.pw_hash):
            auth.store.set_hash(user.user_id, hash_password(body.password))
        auth._start_session(user.user_id, user.role, response)
        auth.audit(request, user.user_id, "login")
        return {
            "user_id": user.user_id,
            "needs_onboarding": auth.store.get_profile(user.user_id) is None,
        }

    @app.post("/v1/auth/refresh")
    def refresh(request: Request, response: Response) -> dict[str, str]:
        raw = request.cookies.get(REFRESH_COOKIE)
        if not raw:
            raise HTTPException(status_code=401, detail="sign in to continue")
        raw_new, hashed_new = new_refresh_token()
        now = auth.clock()
        rotation = auth.store.rotate(
            hash_refresh(raw), hashed_new, now + timedelta(seconds=REFRESH_TTL_S), now
        )
        if rotation.status != "ok" or rotation.session is None:
            if rotation.status == "reuse" and rotation.session:
                auth.audit(request, rotation.session.user_id, "refresh_reuse_session_revoked")
                log.warning("refresh token reuse for user session %s", rotation.session.session_id)
            Auth.clear_cookies(response)
            raise HTTPException(status_code=401, detail="sign in to continue")
        user = auth.store.by_id(rotation.session.user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="sign in to continue")
        auth._cookies(response, user.user_id, user.role, rotation.session.session_id, raw_new)
        return {"user_id": user.user_id}

    @app.post("/v1/auth/logout")
    def logout(request: Request, response: Response) -> dict[str, str]:
        claims = auth.authenticate(request)
        if claims:
            auth.store.revoke_session(claims.session_id)
            auth.audit(request, claims.user_id, "logout")
        Auth.clear_cookies(response)
        return {"status": "signed out"}

    # ---------------------------------------------------------------- the signed-in person
    def me(request: Request) -> Claims:
        claims: Claims = request.state.claims
        return claims

    @app.get("/v1/me")
    def get_me(request: Request) -> dict[str, Any]:
        c = me(request)
        user = auth.store.by_id(c.user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="sign in to continue")
        return {
            "user_id": user.user_id,
            "email": user.email,
            "role": user.role,
            "has_profile": auth.store.get_profile(user.user_id) is not None,
            "consent_version": auth.store.consent(user.user_id),
        }

    @app.get("/v1/me/profile")
    def get_profile(request: Request) -> dict[str, Any]:
        data = auth.store.get_profile(me(request).user_id)
        if data is None:
            raise HTTPException(status_code=404, detail="no profile yet")
        return data

    @app.put("/v1/me/profile")
    def put_profile(body: ProfileIn, request: Request) -> dict[str, str]:
        c = me(request)
        record = body.to_record(c.user_id)
        auth.store.put_profile(c.user_id, stored_form(body))
        auth.store.put_consent(c.user_id, body.consent_version, auth.clock())
        auth.profile_cache.put(record)  # the cache the agents read; the database is the truth
        auth.audit(request, c.user_id, "profile_saved")
        return {"status": "saved"}

    @app.get("/v1/me/export")
    def export(request: Request) -> dict[str, Any]:
        c = me(request)
        user = auth.store.by_id(c.user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="sign in to continue")
        auth.audit(request, c.user_id, "export")
        return {
            "account": {
                "user_id": user.user_id,
                "email": user.email,
                "created_at": user.created_at.isoformat(),
            },
            "profile": auth.store.get_profile(c.user_id),
            "consent_version": auth.store.consent(c.user_id),
            "audit": auth.store.audit_entries(c.user_id),
            "data": auth.export_extra(c.user_id),
        }

    @app.delete("/v1/me")
    def delete_me(body: DeleteAccount, request: Request, response: Response) -> dict[str, str]:
        c = me(request)
        user = auth.store.by_id(c.user_id)
        if user is None or not verify_password(user.pw_hash, body.password):
            raise HTTPException(status_code=403, detail="the password is wrong")
        auth.purge(c.user_id)  # health data in the read models
        auth.store.delete_user(c.user_id)  # account, sessions, profile, consent, audit
        Auth.clear_cookies(response)
        log.info("account deleted: %s", c.user_id)
        return {"status": "deleted"}


# ------------------------------------------------------------------------------- policy
def _authorise(path: str, claims: Claims) -> tuple[int, str] | None:
    """(status, detail) when the request is not allowed, else None."""
    if _ME_PATH.match(path) or path in ("/v1/measurements", "/v1/labs"):
        return None  # own data by construction; body user ids are checked in the route
    m = _USER_PATH.match(path)
    if m:
        return None if m.group(1) == claims.user_id else (404, "not found")  # never confirm others
    if path.startswith("/v1/ops/"):
        return None if claims.role == "operator" else (403, "operators only")
    return (404, "not found")  # deny by default: no rule for this route


def _csrf_ok(request: Request) -> bool:
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        return True  # a bearer header cannot be sent by another site's form or image
    cookie, header = request.cookies.get(CSRF_COOKIE), request.headers.get(CSRF_HEADER)
    return bool(cookie) and bool(header) and secrets.compare_digest(cookie or "", header or "")


def require_self(request: Request, user_id: str) -> None:
    """For routes that carry a user id in the body: it must be the signed-in person's."""
    claims = getattr(request.state, "claims", None)
    if claims is not None and claims.user_id != user_id:
        raise HTTPException(status_code=403, detail="you can only change your own data")


def _secured(request: Request, response: Response, auth: Auth) -> Response:
    nonce = getattr(request.state, "nonce", "")
    h = response.headers
    h["X-Content-Type-Options"] = "nosniff"
    h["Referrer-Policy"] = "no-referrer"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    h["Content-Security-Policy"] = (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; style-src-attr 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    )
    if request.url.path.startswith("/v1/") or request.url.path.startswith("/app"):
        h["Cache-Control"] = "no-store"
    if auth.secure:
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response
