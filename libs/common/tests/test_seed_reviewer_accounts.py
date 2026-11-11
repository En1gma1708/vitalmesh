"""scripts/seed_reviewer_accounts.py: the reviewer's accounts on the deployed demo (D108).

A mocked server stands in for the API, so no stack is needed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "seed_reviewer_accounts.py"
NOW = datetime(2026, 11, 13, 18, 0, tzinfo=UTC)


def load() -> Any:
    spec = importlib.util.spec_from_file_location("seed_reviewer_accounts", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # a dataclass looks its own module up here
    spec.loader.exec_module(mod)
    return mod


class FakeApi:
    def __init__(self, existing: set[str] | None = None, gate: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict[str, Any]] = []
        self.existing = existing or set()
        self.gate = gate
        self.auth_seen: list[str | None] = []
        self.n = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.auth_seen.append(request.headers.get("authorization"))
        path = request.url.path
        self.calls.append((request.method, path))
        body = json.loads(request.content) if request.content else {}
        self.bodies.append(body)
        if request.method == "POST" and path == "/v1/auth/signup":
            if body["email"] in self.existing:
                return httpx.Response(409, json={"detail": "exists"})
            self.n += 1
            return httpx.Response(
                201, json={"user_id": f"u-{self.n}"}, headers={"set-cookie": "vm_csrf=tok; Path=/"}
            )
        if path == "/v1/auth/choices":
            return httpx.Response(200, json={"consent_version": "v-test"})
        if request.method == "PUT" and path == "/v1/me/profile":
            return httpx.Response(200, json={"status": "saved"})
        if request.method == "POST" and path in ("/v1/measurements", "/v1/labs"):
            return httpx.Response(202, json={})
        return httpx.Response(404)


ACCOUNTS = [
    ("steady", "demo.steady@example.org", "Pw-steady-0001"),
    ("weightloss", "demo.weightloss@example.org", "Pw-loss-0002"),
    ("gain", "demo.gain@example.org", "Pw-gain-0003"),
    ("empty", "demo.empty@example.org", "Pw-empty-0004"),
]


def test_three_people_get_data_and_the_empty_account_gets_nothing() -> None:
    mod = load()
    api = FakeApi()
    lines = mod.seed("https://x.example", ACCOUNTS, None, NOW, httpx.MockTransport(api))
    counts = Counter(api.calls)
    assert counts[("POST", "/v1/auth/signup")] == 4
    assert counts[("PUT", "/v1/me/profile")] == 3  # the empty account has no profile
    assert counts[("POST", "/v1/measurements")] == 3 * 14 * 4
    assert counts[("POST", "/v1/labs")] == 1  # only the weight-loss person has a flagged lab
    assert all("FAILED" not in ln for ln in lines)
    assert lines[3].endswith("signed up with nothing else")


def test_each_person_matches_the_story_they_are_meant_to_tell() -> None:
    mod = load()
    from vitalmesh_wellness.bodystatus import BmiCategory, bmi, bmi_category

    def cat(p: Any) -> BmiCategory:
        return bmi_category(bmi(float(p.profile["weight_kg"]), float(p.profile["height_cm"])))

    assert cat(mod.STEADY) is BmiCategory.HEALTHY and mod.STEADY.profile["goal"] == "maintain"
    assert cat(mod.WEIGHTLOSS) is BmiCategory.OVERWEIGHT
    assert mod.WEIGHTLOSS.profile["goal"] == "lose"
    assert mod.WEIGHTLOSS.weight[-1] < mod.WEIGHTLOSS.weight[0]  # falling
    assert cat(mod.GAIN) is BmiCategory.HEALTHY and mod.GAIN.profile["goal"] == "gain"
    assert sum(mod.STEADY.sleep[-7:]) / 7 >= 7.0  # steady sleeps enough
    assert sum(mod.WEIGHTLOSS.sleep[-7:]) / 7 < 7.0  # the weight-loss person is a little short
    assert sum(mod.GAIN.sleep[-7:]) / 7 < 7.0
    assert all(len(getattr(p, f)) == 14 for p in mod.PEOPLE.values()
               for f in ("sleep", "steps", "active", "weight"))  # fmt: skip


def test_every_profile_is_one_the_real_onboarding_accepts() -> None:
    mod = load()
    from vitalmesh_api.onboarding import CONSENT_VERSION, ProfileIn

    for p in mod.PEOPLE.values():
        ProfileIn.model_validate({**p.profile, "consent_version": CONSENT_VERSION})


def test_an_existing_account_is_left_alone() -> None:
    mod = load()
    api = FakeApi(existing={"demo.steady@example.org"})
    lines = mod.seed("https://x.example", ACCOUNTS[:1], None, NOW, httpx.MockTransport(api))
    assert "already exists" in lines[0]
    assert ("PUT", "/v1/me/profile") not in api.calls


def test_the_shared_site_password_is_sent_and_never_printed() -> None:
    mod = load()
    api = FakeApi()
    lines = mod.seed("https://x.example", ACCOUNTS[3:], "SiteGate-secret", NOW,
                     httpx.MockTransport(api))  # fmt: skip
    assert api.auth_seen and all(a and a.startswith("Basic ") for a in api.auth_seen)
    assert not any("SiteGate-secret" in ln or "Pw-empty" in ln for ln in lines)


def test_accounts_file_is_parsed_and_checked(tmp_path: Path) -> None:
    mod = load()
    f = tmp_path / "a.txt"
    f.write_text("# comment\n\nsteady a@example.org p w with spaces\nempty b@example.org pw2\n",
                 encoding="utf-8")  # fmt: skip
    assert mod.read_accounts(f) == [
        ("steady", "a@example.org", "p w with spaces"),
        ("empty", "b@example.org", "pw2"),
    ]
    f.write_text("surprise a@example.org pw\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        mod.read_accounts(f)


@pytest.mark.parametrize(
    ("base", "ok"),
    [
        ("http://127.0.0.1:8000", True),
        ("https://vitalmesh-demo.centralindia.cloudapp.azure.com", True),
        ("http://vitalmesh-demo.centralindia.cloudapp.azure.com", False),  # no plain http
        ("https://evil.example.com", False),
        ("https://x.cloudapp.azure.com.evil.com", False),
    ],
)
def test_only_the_demo_or_loopback_is_accepted(base: str, ok: bool) -> None:
    assert load().allowed_base(base) is ok
