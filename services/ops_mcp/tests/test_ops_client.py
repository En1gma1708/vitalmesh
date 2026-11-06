"""The only door to the system (D88): GET only after sign-in, credentials never leak."""

from __future__ import annotations

import httpx
import pytest
from vitalmesh_ops_mcp.client import HttpOpsApi, OpsError

EMAIL, PASSWORD = "ops@example.org", "correct-horse-battery-staple-9!"


class Api:
    """A scripted API: a session cookie after login, 401 without it (or after it 'expires')."""

    def __init__(self, login_ok: bool = True, role_ok: bool = True) -> None:
        self.log: list[tuple[str, str]] = []
        self.logged_in = False
        self.login_ok, self.role_ok = login_ok, role_ok
        self.expire_next = False
        self.answer: tuple[int, dict[str, object]] = (200, {"ok": True})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.log.append((request.method, request.url.path))
        if request.url.path == "/v1/auth/login":
            if not self.login_ok:
                return httpx.Response(401, json={"detail": "That email and password do not match."})
            self.logged_in = True
            return httpx.Response(200, json={"ok": True})
        if not self.logged_in or self.expire_next:
            self.expire_next = False
            self.logged_in = False
            return httpx.Response(401, json={"detail": "sign in"})
        if not self.role_ok:
            return httpx.Response(403, json={"detail": "operators only"})
        status, body = self.answer
        return httpx.Response(status, json=body)


def client(api: Api) -> HttpOpsApi:
    return HttpOpsApi("http://api.test", EMAIL, PASSWORD, transport=httpx.MockTransport(api))


def test_it_signs_in_on_the_first_401_and_retries_the_request_once() -> None:
    api = Api()
    assert client(api).get("/v1/ops/tiers") == {"ok": True}
    assert api.log == [
        ("GET", "/v1/ops/tiers"),
        ("POST", "/v1/auth/login"),
        ("GET", "/v1/ops/tiers"),
    ]


def test_an_expired_session_is_renewed_once_and_not_in_a_loop() -> None:
    api = Api()
    c = client(api)
    c.get("/v1/ops/tiers")
    api.expire_next = True
    assert c.get("/v1/ops/lag") == {"ok": True}
    assert [m for m, _ in api.log].count("POST") == 2


def test_after_signing_in_only_get_is_ever_sent() -> None:
    api = Api()
    c = client(api)
    for path in ("/v1/ops/tiers", "/v1/ops/runs", "/v1/ops/lag", "/v1/ops/nudges"):
        c.get(path, {"limit": 5} if path.endswith("runs") else None)
    methods = {m for m, p in api.log if p != "/v1/auth/login"}
    assert methods == {"GET"}


def test_a_session_that_will_not_stick_gives_up_after_one_retry() -> None:
    api = Api()
    api.expire_next = True

    class Sticky(Api):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/auth/login":
                self.log.append((request.method, request.url.path))
                return httpx.Response(200, json={})
            self.log.append((request.method, request.url.path))
            return httpx.Response(401, json={})

    sticky = Sticky()
    with pytest.raises(OpsError, match="could not be established"):
        client(sticky).get("/v1/ops/tiers")
    assert len(sticky.log) == 3  # GET, login, GET: no loop


@pytest.mark.parametrize(
    ("status", "expect"),
    [(403, "not an operator"), (404, "not enabled"), (503, "unavailable"), (500, "unexpected")],
)
def test_problems_become_plain_messages(status: int, expect: str) -> None:
    api = Api()
    c = client(api)
    c.get("/v1/ops/tiers")
    if status == 403:
        api.role_ok = False
    else:
        api.answer = (status, {"detail": f"x {expect}"} if status in (404, 503) else {})
    with pytest.raises(OpsError, match=expect):
        c.get("/v1/ops/tiers")


def test_a_refused_sign_in_and_a_dead_api_never_echo_the_password() -> None:
    with pytest.raises(OpsError, match="refused") as refused:
        client(Api(login_ok=False)).get("/v1/ops/tiers")
    assert PASSWORD not in str(refused.value) and EMAIL not in str(refused.value)

    def down(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=_r)

    dead = HttpOpsApi("http://api.test", EMAIL, PASSWORD, transport=httpx.MockTransport(down))
    with pytest.raises(OpsError, match="Is it running") as err:
        dead.get("/v1/ops/tiers")
    assert PASSWORD not in str(err.value)


def test_it_refuses_any_path_outside_the_ops_routes() -> None:
    api = Api()
    for path in ("/v1/me", "/v1/ops", "/v1/users/u-1/labs", "//evil", "/v1/auth/logout"):
        with pytest.raises(OpsError):
            client(api).get(path)
    assert api.log == []  # nothing was even sent


def test_an_oversized_answer_is_refused() -> None:
    api = Api()
    c = client(api)
    c.get("/v1/ops/tiers")
    api.answer = (200, {"x": "y" * 2_100_000})
    with pytest.raises(OpsError, match="larger"):
        c.get("/v1/ops/tiers")
