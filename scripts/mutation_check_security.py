"""Do the security tests actually catch breakage? Break each rule IN MEMORY and expect a failure.

    uv run python scripts/mutation_check_security.py

For every mutation below the API security tests are run with one rule deliberately broken (the
function is replaced in the running process; no file is ever edited). A mutation the tests do NOT
catch is a hole in the tests, and the script exits non-zero. This is a small, hand-picked set of
the rules that matter most, not full mutation testing (that is a separate Phase 7 task).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import pytest
from vitalmesh_accounts import Claims, TokenError
from vitalmesh_api import app as app_module
from vitalmesh_api import security

TESTS = "services/api/tests/test_security.py"


def run_tests() -> int:
    return int(pytest.main(["-q", "-x", "-p", "no:cacheprovider", TESTS]))


class Mutation:
    def __init__(self, name: str, apply: Callable[[], Callable[[], None]]) -> None:
        self.name, self.apply = name, apply


def patch(module: Any, attr: str, replacement: Any) -> Callable[[], None]:
    original = getattr(module, attr)
    setattr(module, attr, replacement)
    return lambda: setattr(module, attr, original)


def authenticate_ignoring_revocation(self: security.Auth, request: Any) -> Claims | None:
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else None
    token = token or request.cookies.get(security.ACCESS_COOKIE)
    if not token:
        return None
    try:
        return self.issuer.verify_access(token, self.clock())
    except TokenError:
        return None


def _no_authorise() -> Callable[[], None]:
    return patch(security, "_authorise", lambda path, claims: None)


def _csrf_always_ok() -> Callable[[], None]:
    return patch(security, "_csrf_ok", lambda request: True)


def _no_body_user_check() -> Callable[[], None]:
    return patch(app_module, "require_self", lambda request, user_id: None)


def _no_revocation() -> Callable[[], None]:
    return patch(security.Auth, "authenticate", authenticate_ignoring_revocation)


def _no_headers() -> Callable[[], None]:
    return patch(security, "_secured", lambda request, response, auth: response)


MUTATIONS = [
    Mutation("any user can read any user's data", _no_authorise),
    Mutation("CSRF check always passes", _csrf_always_ok),
    Mutation("body user id is not compared with the session", _no_body_user_check),
    Mutation("logout does not revoke the session", _no_revocation),
    Mutation("responses carry no security headers", _no_headers),
]


def main() -> None:
    if run_tests() != 0:
        raise SystemExit("the unmodified tests must pass before mutating anything")
    survived: list[str] = []
    for mutation in MUTATIONS:
        undo = mutation.apply()
        try:
            caught = run_tests() != 0
        finally:
            undo()
        print(f"\n>>> {mutation.name}: {'CAUGHT' if caught else 'SURVIVED'}\n")
        if not caught:
            survived.append(mutation.name)
    if run_tests() != 0:
        raise SystemExit("tests fail after restoring: the mutation harness leaked a change")
    print(f"{len(MUTATIONS) - len(survived)} of {len(MUTATIONS)} mutations caught")
    if survived:
        print("NOT caught:", *survived, sep="\n  - ")
        sys.exit(1)


if __name__ == "__main__":
    main()
