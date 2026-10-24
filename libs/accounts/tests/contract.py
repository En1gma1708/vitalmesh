"""Behaviour every AccountStore must have; run against the in-memory twin and Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from vitalmesh_accounts import MAX_FAILURES, AccountStore, EmailTaken

NOW = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)


def check_users(store: AccountStore) -> None:
    u = store.create_user("a@example.com", "hash1")
    assert u.user_id.startswith("u-") and u.role == "user" and "a@" not in u.user_id
    assert store.by_email("a@example.com") == store.by_id(u.user_id)
    with pytest.raises(EmailTaken):
        store.create_user("a@example.com", "hash2")
    assert store.by_email("nobody@example.com") is None


def check_lockout(store: AccountStore) -> None:
    u = store.create_user("b@example.com", "h")
    for _ in range(MAX_FAILURES - 1):
        assert store.record_failure(u.user_id, NOW).locked_until is None
    locked = store.record_failure(u.user_id, NOW)
    assert locked.locked_until is not None and locked.locked_until > NOW
    store.clear_failures(u.user_id)
    after = store.by_id(u.user_id)
    assert after is not None and after.failed_logins == 0 and after.locked_until is None


def check_refresh_rotation(store: AccountStore) -> None:
    u = store.create_user("c@example.com", "h")
    s = store.create_session(u.user_id, "r1", NOW + timedelta(days=14))
    ok = store.rotate("r1", "r2", NOW + timedelta(days=14), NOW)
    assert ok.status == "ok" and ok.session is not None and ok.session.refresh_hash == "r2"
    assert store.rotate("nope", "x", NOW, NOW).status == "unknown"
    # presenting the already-used token again is REUSE: the session is revoked, even the new token
    assert store.rotate("r1", "r3", NOW + timedelta(days=14), NOW).status == "reuse"
    assert store.rotate("r2", "r4", NOW + timedelta(days=14), NOW).status == "revoked"
    revoked = store.session(s.session_id)
    assert revoked is not None and revoked.revoked


def check_expired_and_logout(store: AccountStore) -> None:
    u = store.create_user("d@example.com", "h")
    store.create_session(u.user_id, "e1", NOW - timedelta(seconds=1))
    assert store.rotate("e1", "e2", NOW + timedelta(days=1), NOW).status == "expired"
    s = store.create_session(u.user_id, "e3", NOW + timedelta(days=1))
    store.revoke_session(s.session_id)
    assert store.rotate("e3", "e4", NOW + timedelta(days=1), NOW).status == "revoked"
    s2 = store.create_session(u.user_id, "e5", NOW + timedelta(days=1))
    store.revoke_all(u.user_id)
    revoked = store.session(s2.session_id)
    assert revoked is not None and revoked.revoked


def check_profile_consent_audit_delete(store: AccountStore) -> None:
    u = store.create_user("f@example.com", "h")
    assert store.get_profile(u.user_id) is None and store.consent(u.user_id) is None
    store.put_profile(u.user_id, {"sex": "female", "age_years": 30})
    store.put_profile(u.user_id, {"sex": "female", "age_years": 31})  # update, not a duplicate
    assert store.get_profile(u.user_id) == {"sex": "female", "age_years": 31}
    assert (u.user_id, {"sex": "female", "age_years": 31}) in store.all_profiles()
    store.put_consent(u.user_id, "v1", NOW)
    assert store.consent(u.user_id) == "v1"
    store.audit(u.user_id, "login", "203.0.113.9", {"ok": True})
    assert store.audit_entries(u.user_id)[0]["action"] == "login"
    store.create_session(u.user_id, "z1", NOW + timedelta(days=1))
    store.delete_user(u.user_id)
    assert store.by_id(u.user_id) is None and store.by_email("f@example.com") is None
    assert store.get_profile(u.user_id) is None and store.consent(u.user_id) is None
    assert store.audit_entries(u.user_id) == []
    assert store.rotate("z1", "z2", NOW, NOW).status == "unknown"


def check_action_tokens(store: AccountStore) -> None:
    u = store.create_user("g@example.com", "h")
    other = store.create_user("h@example.com", "h")
    assert u.email_verified is False
    exp = NOW + timedelta(minutes=30)
    store.add_action_token(u.user_id, "reset", "t1", exp)
    assert store.consume_action_token("t1", "verify", NOW) is None  # wrong purpose
    assert store.consume_action_token("t1", "reset", NOW + timedelta(minutes=31)) is None  # expired
    assert store.consume_action_token("t1", "reset", NOW) == u.user_id
    assert store.consume_action_token("t1", "reset", NOW) is None  # single use
    assert store.consume_action_token("never-issued", "reset", NOW) is None
    # a newer link for the same person and purpose replaces the older one; others are untouched
    store.add_action_token(u.user_id, "reset", "t2", exp)
    store.add_action_token(u.user_id, "reset", "t3", exp)
    store.add_action_token(other.user_id, "reset", "t4", exp)
    assert store.consume_action_token("t2", "reset", NOW) is None
    assert store.consume_action_token("t3", "reset", NOW) == u.user_id
    assert store.consume_action_token("t4", "reset", NOW) == other.user_id
    store.mark_email_verified(u.user_id)
    store.mark_email_verified(u.user_id)  # idempotent
    after, untouched = store.by_id(u.user_id), store.by_id(other.user_id)
    assert after is not None and after.email_verified is True
    assert untouched is not None and untouched.email_verified is False
    store.add_action_token(u.user_id, "verify", "t5", exp)
    store.delete_user(u.user_id)
    assert store.consume_action_token("t5", "verify", NOW) is None  # gone with the account


CHECKS = [
    check_action_tokens,
    check_users,
    check_lockout,
    check_refresh_rotation,
    check_expired_and_logout,
    check_profile_consent_audit_delete,
]
