from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from contract import CHECKS
from vitalmesh_accounts import (
    Claims,
    InMemoryAccountStore,
    InMemoryRateLimiter,
    TokenError,
    TokenIssuer,
    check_email,
    check_password,
    hash_password,
    needs_rehash,
    new_refresh_token,
    secret_from_env,
    verify_password,
)

NOW = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
SECRET = b"x" * 32


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
def test_in_memory_store_meets_the_contract(check) -> None:  # type: ignore[no-untyped-def]
    check(InMemoryAccountStore())


# ------------------------------------------------------------------------------- passwords
def test_hash_verify_and_unknown_account_still_spends_the_time() -> None:
    h = hash_password("correct horse battery")
    assert h.startswith("$argon2id$") and verify_password(h, "correct horse battery")
    assert not verify_password(h, "wrong password!!")
    assert not verify_password(None, "anything at all")  # unknown account: False, no shortcut
    assert not verify_password("not-a-hash", "x")
    assert not needs_rehash(h)


@pytest.mark.parametrize(
    ("password", "email", "fragment"),
    [
        ("short", "a@example.com", "at least"),
        ("x" * 129, "a@example.com", "at most"),
        ("password123", "a@example.com", "too common"),
        ("sahilsharma-secret", "sahilsharma@example.com", "email"),
    ],
)
def test_password_policy_gives_plain_reasons(password: str, email: str, fragment: str) -> None:
    assert any(fragment in p for p in check_password(password, email))


def test_a_long_passphrase_with_no_symbols_is_fine() -> None:
    assert check_password("correct horse battery staple", "a@example.com") == []


@pytest.mark.parametrize("email", ["", "no-at-sign", "a@b", "a b@example.com", "@example.com"])
def test_bad_emails_are_rejected(email: str) -> None:
    assert check_email(email) is not None
    assert check_email("User@Example.com ") is None


# --------------------------------------------------------------------------------- tokens
def test_access_token_round_trip_and_expiry() -> None:
    issuer = TokenIssuer(SECRET, access_ttl_s=60)
    token = issuer.access(Claims("u-1", "user", "s-1"), NOW)
    assert issuer.verify_access(token, NOW + timedelta(seconds=30)) == Claims("u-1", "user", "s-1")
    with pytest.raises(TokenError, match="expired"):
        issuer.verify_access(token, NOW + timedelta(seconds=61))


def test_tampered_foreign_and_none_algorithm_tokens_are_refused() -> None:
    issuer = TokenIssuer(SECRET)
    good = issuer.access(Claims("u-1", "user", "s-1"), NOW)
    body = good.split(".")
    with pytest.raises(TokenError):
        issuer.verify_access(".".join([body[0], body[1], "A" * len(body[2])]), NOW)
    other = TokenIssuer(b"y" * 32).access(Claims("u-1", "operator", "s-1"), NOW)
    with pytest.raises(TokenError):
        issuer.verify_access(other, NOW)
    claims = {
        "iss": "vitalmesh",
        "sub": "u-1",
        "role": "operator",
        "sid": "s",
        "iat": 1,
        "exp": 4102444800,
    }
    forged = jwt.encode(claims, key="", algorithm="none")
    with pytest.raises(TokenError):
        issuer.verify_access(forged, NOW)


def test_an_unknown_role_is_refused_even_with_a_valid_signature() -> None:
    issuer = TokenIssuer(SECRET)
    claims = {
        "iss": "vitalmesh",
        "sub": "u-1",
        "role": "root",
        "sid": "s",
        "iat": int(NOW.timestamp()),
        "exp": int(NOW.timestamp()) + 60,
    }
    with pytest.raises(TokenError, match="claims"):
        issuer.verify_access(jwt.encode(claims, SECRET, algorithm="HS256"), NOW)


def test_weak_secrets_are_refused_and_a_missing_one_is_never_a_fixed_default() -> None:
    with pytest.raises(ValueError):
        TokenIssuer(b"short")
    with pytest.raises(ValueError):
        secret_from_env({"VITALMESH_AUTH_SECRET": "too short"})
    a, b = secret_from_env({}), secret_from_env({})
    assert a != b and len(a) >= 32  # random per process


def test_refresh_tokens_are_random_and_stored_only_as_hashes() -> None:
    raw1, h1 = new_refresh_token()
    raw2, _ = new_refresh_token()
    assert raw1 != raw2 and raw1 not in h1 and len(h1) == 64


# ------------------------------------------------------------------------------ rate limit
def test_rate_limiter_allows_the_limit_then_blocks_until_the_window_passes() -> None:
    now = [100.0]
    rl = InMemoryRateLimiter(clock=lambda: now[0])
    assert [rl.hit("k", 3, 60) for _ in range(5)] == [True, True, True, False, False]
    assert rl.hit("other", 3, 60)
    now[0] += 61
    assert rl.hit("k", 3, 60)
    rl.reset("k")
    assert rl.hit("k", 1, 60)
