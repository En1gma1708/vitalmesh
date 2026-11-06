"""The only door from the Ops MCP server to the system: GET requests to the API's own /v1/ops
routes, signed in as an operator (D88).

Why through the API and not straight to Redis or Kafka: the API already decides who may see
operator data (the operator role), already removes question and answer text, and already audits.
A second path with its own copy of those rules is how a back door is born, so there is none.

The credentials come from the environment, are used only to sign in and are never printed, logged
or put in an error message. The session is kept in memory; when it expires (HTTP 401) the client
signs in once more and retries the request once. Only GET is ever sent after sign-in.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

TIMEOUT_S = 15.0
MAX_BODY_BYTES = 2_000_000


class OpsError(RuntimeError):
    """A problem the person using the tool can act on; the message is plain and safe to show."""


class OpsApi(Protocol):
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any: ...


class HttpOpsApi:
    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._email, self._password = email, password
        self._client = httpx.Client(base_url=base_url, timeout=TIMEOUT_S, transport=transport)

    def _sign_in(self) -> None:
        try:
            r = self._client.post(
                "/v1/auth/login", json={"email": self._email, "password": self._password}
            )
        except httpx.HTTPError:
            raise OpsError("The VitalMesh API did not answer. Is it running?") from None
        if r.status_code == 429:
            raise OpsError("The API is rate limiting sign-ins; try again in a few minutes.")
        if r.status_code != 200:
            raise OpsError("The operator sign-in was refused (check the configured account).")

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not path.startswith("/v1/ops/"):
            raise OpsError("This server only reads /v1/ops routes.")  # a bug, not a user error
        for attempt in (0, 1):
            try:
                r = self._client.get(path, params=params)
            except httpx.HTTPError:
                raise OpsError("The VitalMesh API did not answer. Is it running?") from None
            if r.status_code == 401 and attempt == 0:
                self._sign_in()
                continue
            break
        if r.status_code == 401:
            raise OpsError("The operator session could not be established.")
        if r.status_code == 403:
            raise OpsError("That account is not an operator, so it cannot read ops data.")
        if r.status_code == 404:
            raise OpsError(_detail(r) or "Not found, or not enabled on this deployment.")
        if r.status_code == 503:
            raise OpsError(_detail(r) or "The backend for that view is unavailable.")
        if r.status_code != 200:
            raise OpsError(f"The API answered with an unexpected status ({r.status_code}).")
        if len(r.content) > MAX_BODY_BYTES:
            raise OpsError("The API answer was larger than this server accepts.")
        return r.json()

    def close(self) -> None:
        self._client.close()


def _detail(r: httpx.Response) -> str | None:
    try:
        d = r.json().get("detail")
    except (ValueError, AttributeError):
        return None
    return d[:200] if isinstance(d, str) else None
