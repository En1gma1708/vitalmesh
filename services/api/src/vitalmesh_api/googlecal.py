"""Connect a person's Google Calendar (D80).

What it does and does not do:

* ONE scope, ``calendar.events``: list events (to find free time) and create events. Event titles
  and descriptions of the person's own events are read only to be discarded; only start and end
  times are kept, and only for the length of one request.
* The authorization-code flow with PKCE (S256) and a single-use ``state`` bound to the signed-in
  person's session (that binding lives in ``integrations.py``).
* Only the REFRESH token is stored, encrypted at rest (Fernet). Access tokens are fetched when
  needed and never stored. A token that no longer works (revoked, or Google's 7-day limit for apps
  in testing mode) raises ``Disconnected`` and the person is asked to connect again.
* Tokens, codes and secrets are never logged and never put in an error message.

Google's endpoints and the ``calendar.events`` scope for ``events.list`` and ``events.insert`` come
from Google's published documentation; PKCE for a web-application client and the revoke endpoint
are used as documented for OAuth 2.0 but were not exercised against Google yet (see D80).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from vitalmesh_accounts.keyvault import fetch_secret

log = logging.getLogger("vitalmesh.google")

PROVIDER = "google"
SCOPE = "https://www.googleapis.com/auth/calendar.events"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
MAX_PAGES = 5


class GoogleError(Exception):
    """A failure with a message that is safe to show the person (no tokens, no details)."""


class Disconnected(GoogleError):
    """The stored permission no longer works; the person must connect again."""


@dataclass(frozen=True)
class GoogleConfig:
    client_id: str
    client_secret: str
    redirect_uri: str


def _env_file_value(path: Path, key: str) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == key:
                return value.strip().strip("\"'")
    except OSError:
        pass
    return ""


def config_from_env(base_url: str, env_file: Path | None = None) -> GoogleConfig | None:
    """The client id and secret come from the environment, then the gitignored ``.env``; the
    redirect address is derived from the server's own origin. None when not configured."""
    path = env_file or Path(".env")
    client_id = os.environ.get("GOOGLE_CLIENT_ID") or _env_file_value(path, "GOOGLE_CLIENT_ID")
    secret = os.environ.get("GOOGLE_CLIENT_SECRET") or _env_file_value(path, "GOOGLE_CLIENT_SECRET")
    if not client_id or not secret:
        return None
    return GoogleConfig(
        client_id, secret, base_url.rstrip("/") + "/v1/integrations/google/callback"
    )


# ------------------------------------------------------------------------- token at rest
class TokenUnreadable(GoogleError):
    pass


class TokenBox:
    """Encrypts the refresh token. The key is ``VITALMESH_TOKEN_KEY`` (a Fernet key); without it
    a random key is made for this process only, with a loud warning: stored connections then
    cannot be read after a restart and people must connect again (safe, not silent)."""

    def __init__(self, key: bytes | None = None) -> None:
        if key is None:
            log.warning(
                "VITALMESH_TOKEN_KEY is not set: using a random key for this process; stored "
                "Google connections will not survive a restart"
            )
            key = Fernet.generate_key()
        self._fernet = Fernet(key)

    @classmethod
    def from_env(cls) -> TokenBox:
        raw = os.environ.get("VITALMESH_TOKEN_KEY", "")
        ref = os.environ.get("VITALMESH_TOKEN_KEY_KEYVAULT", "")
        if not raw and ref:
            raw = fetch_secret(ref)  # D92: a vault failure stops the process, no random key
        raw = raw or _env_file_value(Path(".env"), "VITALMESH_TOKEN_KEY")
        return cls(raw.encode() if raw else None)

    def seal(self, text: str) -> str:
        return self._fernet.encrypt(text.encode()).decode()

    def open(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            raise TokenUnreadable("Please connect Google Calendar again.") from None


# ------------------------------------------------------------------------------- PKCE
def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge) per RFC 7636."""
    verifier = secrets.token_urlsafe(64)  # 86 characters, within the allowed 43-128
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def event_id(user_id: str, start: datetime) -> str:
    """A stable id for an event we create (Google allows 0-9 and a-v, 5-1024 characters): the same
    person and start time always give the same id, so a repeated confirmation cannot duplicate."""
    digest = hashlib.sha256(f"vitalmesh:{user_id}:{start.astimezone(UTC).isoformat()}".encode())
    return base64.b32hexencode(digest.digest()[:15]).decode().lower().rstrip("=")


# ------------------------------------------------------------------------------ client
class GoogleClient:
    def __init__(self, cfg: GoogleConfig, http: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self._http = http or httpx.Client(timeout=15)

    def authorize_url(self, state: str, challenge: str) -> str:
        return (
            AUTH_URL
            + "?"
            + urlencode(
                {
                    "client_id": self.cfg.client_id,
                    "redirect_uri": self.cfg.redirect_uri,
                    "response_type": "code",
                    "scope": SCOPE,
                    "access_type": "offline",
                    "prompt": "consent",  # a refresh token is only issued with consent
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )

    def _post(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        try:
            r = self._http.post(url, data=data)
        except httpx.HTTPError:
            raise GoogleError("Google could not be reached. Please try again.") from None
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 400:
            if isinstance(body, dict) and body.get("error") == "invalid_grant":
                raise Disconnected("Please connect Google Calendar again.")
            log.warning("google token endpoint answered %s", r.status_code)  # status only
            raise GoogleError("Google did not accept that. Please try connecting again.")
        return body if isinstance(body, dict) else {}

    def exchange(self, code: str, verifier: str) -> tuple[str, str]:
        """(refresh token, granted scope) for an authorization code."""
        body = self._post(
            TOKEN_URL,
            {
                "code": code,
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
                "redirect_uri": self.cfg.redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
        )
        refresh, scope = body.get("refresh_token"), str(body.get("scope", ""))
        if not isinstance(refresh, str) or not refresh:
            raise GoogleError("Google did not give long-lived access. Please try again.")
        if SCOPE not in scope.split():
            raise GoogleError("Calendar access was not granted, so nothing was connected.")
        return refresh, SCOPE

    def access_token(self, refresh_token: str) -> str:
        body = self._post(
            TOKEN_URL,
            {
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise GoogleError("Google did not accept that. Please try connecting again.")
        return token

    def revoke(self, token: str) -> None:
        """Best effort: if Google cannot be reached the local copy is deleted anyway."""
        try:
            self._http.post(REVOKE_URL, data={"token": token})
        except httpx.HTTPError:
            log.warning("could not reach google to revoke a token")

    def busy(
        self, access: str, time_min: datetime, time_max: datetime
    ) -> list[tuple[datetime, datetime]]:
        """Start and end of timed events that take up time. All-day events, cancelled events and
        events marked 'free' do not block. Titles and descriptions are not read into memory."""
        out: list[tuple[datetime, datetime]] = []
        params: dict[str, str] = {
            "timeMin": time_min.astimezone(UTC).isoformat(),
            "timeMax": time_max.astimezone(UTC).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "250",
            "fields": "nextPageToken,items(start,end,status,transparency)",
        }
        for _ in range(MAX_PAGES):
            try:
                r = self._http.get(
                    EVENTS_URL, params=params, headers={"Authorization": f"Bearer {access}"}
                )
            except httpx.HTTPError:
                raise GoogleError("Google Calendar could not be reached.") from None
            if r.status_code in (401, 403):
                raise Disconnected("Please connect Google Calendar again.")
            if r.status_code >= 400:
                raise GoogleError("Google Calendar did not answer. Please try again.")
            body = r.json()
            for item in body.get("items", []):
                if item.get("status") == "cancelled" or item.get("transparency") == "transparent":
                    continue
                start, end = item.get("start", {}), item.get("end", {})
                if "dateTime" not in start or "dateTime" not in end:
                    continue  # an all-day event
                out.append(
                    (
                        datetime.fromisoformat(start["dateTime"]),
                        datetime.fromisoformat(end["dateTime"]),
                    )
                )
            token = body.get("nextPageToken")
            if not token:
                return out
            params["pageToken"] = token
        raise GoogleError("That calendar is too busy to read. Try a shorter period.")

    def create_event(
        self,
        access: str,
        ident: str,
        title: str,
        description: str,
        start: datetime,
        end: datetime,
        tz_name: str,
    ) -> bool:
        """True if created, False if an event with this id already exists (a repeat)."""
        body = {
            "id": ident,
            "summary": title,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
            "end": {"dateTime": end.isoformat(), "timeZone": tz_name},
            "extendedProperties": {"private": {"vitalmesh": "1"}},
        }
        try:
            r = self._http.post(
                EVENTS_URL, json=body, headers={"Authorization": f"Bearer {access}"}
            )
        except httpx.HTTPError:
            raise GoogleError("Google Calendar could not be reached.") from None
        if r.status_code == 409:
            return False
        if r.status_code in (401, 403):
            raise Disconnected("Please connect Google Calendar again.")
        if r.status_code >= 400:
            raise GoogleError("Google Calendar did not accept the event. Please try again.")
        return True


def intervals_overlap(
    a: tuple[datetime, datetime], others: Iterable[tuple[datetime, datetime]]
) -> bool:
    return any(a[0] < o[1] and o[0] < a[1] for o in others)
