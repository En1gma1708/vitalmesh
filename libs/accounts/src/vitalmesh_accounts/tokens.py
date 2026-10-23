"""Session tokens: a short-lived signed access token and a rotating refresh token (D75).

* Access token: a JWT (HS256, algorithm pinned on verify so an attacker cannot choose ``none`` or
  switch to a key-confusion algorithm), 15 minutes, carrying the user id, role and session id.
  Nothing else: no email, no profile data.
* Refresh token: 32 random bytes; only its SHA-256 is stored, so a database leak does not yield
  usable sessions. It is single-use: each refresh issues a new one and the old hash is kept as
  ``previous`` so that REUSE of a stolen, already-used token is detected and the whole session is
  revoked (the store does this; see ``AccountStore.rotate``).
* The signing secret comes from the environment (``VITALMESH_AUTH_SECRET``, at least 32 bytes). If
  it is missing the process may generate a throwaway one for local development and says so loudly;
  it never falls back to a fixed default.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

log = logging.getLogger("vitalmesh.accounts")

ACCESS_TTL_S = 15 * 60
REFRESH_TTL_S = 14 * 24 * 3600
ISSUER = "vitalmesh"
ALGORITHM = "HS256"
MIN_SECRET_BYTES = 32
ROLES = ("user", "clinician", "operator")


class TokenError(Exception):
    """The token is missing, malformed, expired or not ours. The reason is for logs, not users."""


@dataclass(frozen=True)
class Claims:
    user_id: str
    role: str
    session_id: str


def secret_from_env(env: dict[str, str] | None = None) -> bytes:
    source = os.environ if env is None else env
    raw = source.get("VITALMESH_AUTH_SECRET", "")
    if raw:
        if len(raw.encode()) < MIN_SECRET_BYTES:
            raise ValueError(f"VITALMESH_AUTH_SECRET must be at least {MIN_SECRET_BYTES} bytes")
        return raw.encode()
    log.warning(
        "VITALMESH_AUTH_SECRET is not set: using a random secret for this process only, so "
        "every login ends when it restarts. Set it before running anywhere that matters."
    )
    return secrets.token_bytes(MIN_SECRET_BYTES)


class TokenIssuer:
    def __init__(self, secret: bytes, access_ttl_s: int = ACCESS_TTL_S) -> None:
        if len(secret) < MIN_SECRET_BYTES:
            raise ValueError(f"the signing secret must be at least {MIN_SECRET_BYTES} bytes")
        self._secret, self._ttl = secret, access_ttl_s

    def access(self, claims: Claims, now: datetime | None = None) -> str:
        issued = now or datetime.now(UTC)
        payload = {
            "iss": ISSUER,
            "sub": claims.user_id,
            "role": claims.role,
            "sid": claims.session_id,
            "iat": int(issued.timestamp()),
            "exp": int((issued + timedelta(seconds=self._ttl)).timestamp()),
        }
        return jwt.encode(payload, self._secret, algorithm=ALGORITHM)

    def verify_access(self, token: str, now: datetime | None = None) -> Claims:
        try:
            data = jwt.decode(
                token,
                self._secret,
                algorithms=[ALGORITHM],  # pinned: the token cannot pick its own algorithm
                issuer=ISSUER,
                # expiry is checked below against an injectable clock
                options={
                    "require": ["exp", "iat", "sub", "iss"],
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
        except jwt.PyJWTError as exc:
            raise TokenError(type(exc).__name__) from exc
        if int(data["exp"]) <= int((now or datetime.now(UTC)).timestamp()):
            raise TokenError("expired")
        role = data.get("role")
        if role not in ROLES or not data.get("sid"):
            raise TokenError("claims")
        return Claims(user_id=str(data["sub"]), role=str(role), session_id=str(data["sid"]))


def new_refresh_token() -> tuple[str, str]:
    """(raw token for the cookie, hash to store)."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_refresh(raw)


def hash_refresh(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
