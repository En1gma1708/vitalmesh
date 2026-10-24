"""The account store: users, sessions, profiles, consent and the audit log (D75).

Postgres is the source of truth (durable, unlike the Redis read models the agents use). An
in-memory twin with the same behaviour backs the tests. Everything a login needs is here so the
API layer stays thin and the security rules are testable without HTTP:

* email is unique after normalisation; the user id is random (``u-`` + 16 hex), never derived from
  the email, so ids leak nothing;
* a refresh token is single-use; presenting an already-rotated one is REUSE (theft or a replayed
  copy) and revokes the whole session;
* repeated failed logins lock the account for a while (``record_failure``);
* a one-time link token (email verification, password reset) is stored only as a hash, expires,
  can be used once, and a newer one for the same person and purpose replaces the older one;
* the audit log is append-only from the application's side and holds no secrets, no passwords and
  no health data: who did what, when, from which address.
"""

from __future__ import annotations

import secrets
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

MAX_FAILURES = 5
LOCK_FOR = timedelta(minutes=15)
PURPOSES = ("verify", "reset")


class EmailTaken(Exception):
    pass


@dataclass(frozen=True)
class User:
    user_id: str
    email: str
    pw_hash: str
    role: str = "user"
    failed_logins: int = 0
    locked_until: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    email_verified: bool = False


@dataclass(frozen=True)
class Session:
    session_id: str
    user_id: str
    refresh_hash: str
    previous_hash: str | None
    expires_at: datetime
    revoked: bool = False


@dataclass(frozen=True)
class Rotation:
    """Result of presenting a refresh token."""

    status: str  # "ok" | "unknown" | "expired" | "revoked" | "reuse"
    session: Session | None = None


def new_user_id() -> str:
    return "u-" + secrets.token_hex(8)


class AccountStore(Protocol):
    def create_user(self, email: str, pw_hash: str, role: str = "user") -> User: ...
    def by_email(self, email: str) -> User | None: ...
    def by_id(self, user_id: str) -> User | None: ...
    def record_failure(self, user_id: str, now: datetime) -> User: ...
    def clear_failures(self, user_id: str) -> None: ...
    def set_hash(self, user_id: str, pw_hash: str) -> None: ...
    def create_session(self, user_id: str, refresh_hash: str, expires_at: datetime) -> Session: ...
    def session(self, session_id: str) -> Session | None: ...
    def rotate(
        self, presented_hash: str, new_hash: str, new_expiry: datetime, now: datetime
    ) -> Rotation: ...
    def revoke_session(self, session_id: str) -> None: ...
    def revoke_all(self, user_id: str) -> None: ...
    def put_profile(self, user_id: str, data: dict[str, Any]) -> None: ...
    def get_profile(self, user_id: str) -> dict[str, Any] | None: ...
    def all_profiles(self) -> list[tuple[str, dict[str, Any]]]: ...
    def put_consent(self, user_id: str, version: str, at: datetime) -> None: ...
    def consent(self, user_id: str) -> str | None: ...
    def audit(self, user_id: str | None, action: str, ip: str, detail: dict[str, Any]) -> None: ...
    def audit_entries(self, user_id: str) -> list[dict[str, Any]]: ...
    def delete_user(self, user_id: str) -> None: ...
    def add_action_token(
        self, user_id: str, purpose: str, token_hash: str, expires_at: datetime
    ) -> None: ...
    def consume_action_token(self, token_hash: str, purpose: str, now: datetime) -> str | None: ...
    def mark_email_verified(self, user_id: str) -> None: ...


# --------------------------------------------------------------------------------- in memory
class InMemoryAccountStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._users: dict[str, User] = {}
        self._by_email: dict[str, str] = {}
        self._sessions: dict[str, Session] = {}
        self._profiles: dict[str, dict[str, Any]] = {}
        self._consent: dict[str, str] = {}
        self._audit: list[dict[str, Any]] = []
        # token_hash -> (user_id, purpose, expires_at, used)
        self._tokens: dict[str, tuple[str, str, datetime, bool]] = {}

    def create_user(self, email: str, pw_hash: str, role: str = "user") -> User:
        with self._lock:
            if email in self._by_email:
                raise EmailTaken(email)
            user = User(new_user_id(), email, pw_hash, role)
            self._users[user.user_id] = user
            self._by_email[email] = user.user_id
            return user

    def by_email(self, email: str) -> User | None:
        uid = self._by_email.get(email)
        return self._users.get(uid) if uid else None

    def by_id(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    def record_failure(self, user_id: str, now: datetime) -> User:
        with self._lock:
            user = self._users[user_id]
            failures = user.failed_logins + 1
            locked = now + LOCK_FOR if failures >= MAX_FAILURES else user.locked_until
            self._users[user_id] = replace(user, failed_logins=failures, locked_until=locked)
            return self._users[user_id]

    def clear_failures(self, user_id: str) -> None:
        with self._lock:
            self._users[user_id] = replace(self._users[user_id], failed_logins=0, locked_until=None)

    def set_hash(self, user_id: str, pw_hash: str) -> None:
        with self._lock:
            self._users[user_id] = replace(self._users[user_id], pw_hash=pw_hash)

    def create_session(self, user_id: str, refresh_hash: str, expires_at: datetime) -> Session:
        with self._lock:
            s = Session(str(uuid.uuid4()), user_id, refresh_hash, None, expires_at)
            self._sessions[s.session_id] = s
            return s

    def session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def rotate(
        self, presented_hash: str, new_hash: str, new_expiry: datetime, now: datetime
    ) -> Rotation:
        with self._lock:
            for s in self._sessions.values():
                if s.previous_hash == presented_hash:
                    # an already-used token came back: someone replayed it. End the session.
                    self._sessions[s.session_id] = replace(s, revoked=True)
                    return Rotation("reuse", s)
            current = next(
                (s for s in self._sessions.values() if s.refresh_hash == presented_hash), None
            )
            if current is None:
                return Rotation("unknown")
            if current.revoked:
                return Rotation("revoked", current)
            if current.expires_at <= now:
                return Rotation("expired", current)
            updated = replace(
                current, refresh_hash=new_hash, previous_hash=presented_hash, expires_at=new_expiry
            )
            self._sessions[current.session_id] = updated
            return Rotation("ok", updated)

    def revoke_session(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id] = replace(self._sessions[session_id], revoked=True)

    def revoke_all(self, user_id: str) -> None:
        with self._lock:
            for sid, s in list(self._sessions.items()):
                if s.user_id == user_id:
                    self._sessions[sid] = replace(s, revoked=True)

    def put_profile(self, user_id: str, data: dict[str, Any]) -> None:
        self._profiles[user_id] = dict(data)

    def get_profile(self, user_id: str) -> dict[str, Any] | None:
        return dict(self._profiles[user_id]) if user_id in self._profiles else None

    def all_profiles(self) -> list[tuple[str, dict[str, Any]]]:
        return [(u, dict(p)) for u, p in self._profiles.items()]

    def put_consent(self, user_id: str, version: str, at: datetime) -> None:
        self._consent[user_id] = version

    def consent(self, user_id: str) -> str | None:
        return self._consent.get(user_id)

    def audit(self, user_id: str | None, action: str, ip: str, detail: dict[str, Any]) -> None:
        self._audit.append(
            {"at": datetime.now(UTC).isoformat(), "user_id": user_id, "action": action,
             "ip": ip, "detail": detail}
        )  # fmt: skip

    def audit_entries(self, user_id: str) -> list[dict[str, Any]]:
        return [e for e in self._audit if e["user_id"] == user_id]

    def delete_user(self, user_id: str) -> None:
        with self._lock:
            user = self._users.pop(user_id, None)
            if user:
                self._by_email.pop(user.email, None)
            for sid in [k for k, s in self._sessions.items() if s.user_id == user_id]:
                del self._sessions[sid]
            self._profiles.pop(user_id, None)
            self._consent.pop(user_id, None)
            self._tokens = {h: t for h, t in self._tokens.items() if t[0] != user_id}
            # the audit trail keeps the fact of deletion but no longer identifies the account's data
            self._audit = [e for e in self._audit if e["user_id"] != user_id]

    def add_action_token(
        self, user_id: str, purpose: str, token_hash: str, expires_at: datetime
    ) -> None:
        with self._lock:
            self._tokens = {
                h: t for h, t in self._tokens.items() if not (t[0] == user_id and t[1] == purpose)
            }
            self._tokens[token_hash] = (user_id, purpose, expires_at, False)

    def consume_action_token(self, token_hash: str, purpose: str, now: datetime) -> str | None:
        with self._lock:
            entry = self._tokens.get(token_hash)
            if entry is None:
                return None
            user_id, kind, expires_at, used = entry
            if kind != purpose or used or expires_at <= now:
                return None
            self._tokens[token_hash] = (user_id, kind, expires_at, True)
            return user_id

    def mark_email_verified(self, user_id: str) -> None:
        with self._lock:
            if user_id in self._users:
                self._users[user_id] = replace(self._users[user_id], email_verified=True)


# ----------------------------------------------------------------------------------- postgres
MIGRATION = """
CREATE TABLE IF NOT EXISTS users (
  user_id text PRIMARY KEY,
  email text NOT NULL UNIQUE,
  pw_hash text NOT NULL,
  role text NOT NULL DEFAULT 'user',
  failed_logins int NOT NULL DEFAULT 0,
  locked_until timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified_at timestamptz;
CREATE TABLE IF NOT EXISTS action_tokens (
  token_hash text PRIMARY KEY,
  user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  purpose text NOT NULL,
  expires_at timestamptz NOT NULL,
  used boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS sessions (
  session_id text PRIMARY KEY,
  user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  refresh_hash text NOT NULL,
  previous_hash text,
  expires_at timestamptz NOT NULL,
  revoked boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS sessions_refresh ON sessions (refresh_hash);
CREATE INDEX IF NOT EXISTS sessions_previous ON sessions (previous_hash);
CREATE TABLE IF NOT EXISTS profiles (
  user_id text PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
  data jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS consents (
  user_id text PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
  version text NOT NULL,
  accepted_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id bigserial PRIMARY KEY,
  at timestamptz NOT NULL DEFAULT now(),
  user_id text,
  action text NOT NULL,
  ip text NOT NULL,
  detail jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS audit_user ON audit (user_id, at);
"""


def _user(row: Any) -> User:
    return User(row[0], row[1], row[2], row[3], row[4], row[5], row[6], bool(row[7]))


def _session(row: Any) -> Session:
    return Session(row[0], row[1], row[2], row[3], row[4], row[5])


class PostgresAccountStore:
    def __init__(self, dsn: str, max_connections: int = 10) -> None:
        self._pool = ConnectionPool(dsn, min_size=1, max_size=max_connections, open=True)
        with self._pool.connection() as conn:
            conn.execute(MIGRATION)

    def close(self) -> None:
        self._pool.close()

    def _one(self, sql: str, args: tuple[Any, ...] = ()) -> Any:
        with self._pool.connection() as conn:
            return conn.execute(sql, args).fetchone()

    def _run(self, sql: str, args: tuple[Any, ...] = ()) -> None:
        with self._pool.connection() as conn:
            conn.execute(sql, args)

    _USER = (
        "user_id, email, pw_hash, role, failed_logins, locked_until, created_at, "
        "email_verified_at IS NOT NULL"
    )
    _SESSION = "session_id, user_id, refresh_hash, previous_hash, expires_at, revoked"

    def create_user(self, email: str, pw_hash: str, role: str = "user") -> User:
        uid = new_user_id()
        try:
            row = self._one(
                f"INSERT INTO users (user_id, email, pw_hash, role) VALUES (%s,%s,%s,%s) "  # nosec B608 - constant column list, values are bound parameters
                f"RETURNING {self._USER}",
                (uid, email, pw_hash, role),
            )
        except UniqueViolation as exc:
            raise EmailTaken(email) from exc
        return _user(row)

    def by_email(self, email: str) -> User | None:
        row = self._one(f"SELECT {self._USER} FROM users WHERE email=%s", (email,))  # nosec B608 - constant column list, values are bound parameters
        return _user(row) if row else None

    def by_id(self, user_id: str) -> User | None:
        row = self._one(f"SELECT {self._USER} FROM users WHERE user_id=%s", (user_id,))  # nosec B608 - constant column list, values are bound parameters
        return _user(row) if row else None

    def record_failure(self, user_id: str, now: datetime) -> User:
        row = self._one(
            f"UPDATE users SET failed_logins = failed_logins + 1, "  # nosec B608 - constant column list, values are bound parameters
            f"locked_until = CASE WHEN failed_logins + 1 >= %s THEN %s ELSE locked_until END "
            f"WHERE user_id=%s RETURNING {self._USER}",
            (MAX_FAILURES, now + LOCK_FOR, user_id),
        )
        return _user(row)

    def clear_failures(self, user_id: str) -> None:
        self._run(
            "UPDATE users SET failed_logins=0, locked_until=NULL WHERE user_id=%s", (user_id,)
        )

    def set_hash(self, user_id: str, pw_hash: str) -> None:
        self._run("UPDATE users SET pw_hash=%s WHERE user_id=%s", (pw_hash, user_id))

    def create_session(self, user_id: str, refresh_hash: str, expires_at: datetime) -> Session:
        row = self._one(
            f"INSERT INTO sessions (session_id, user_id, refresh_hash, expires_at) "  # nosec B608 - constant column list, values are bound parameters
            f"VALUES (%s,%s,%s,%s) RETURNING {self._SESSION}",
            (str(uuid.uuid4()), user_id, refresh_hash, expires_at),
        )
        return _session(row)

    def session(self, session_id: str) -> Session | None:
        row = self._one(f"SELECT {self._SESSION} FROM sessions WHERE session_id=%s", (session_id,))  # nosec B608 - constant column list, values are bound parameters
        return _session(row) if row else None

    def rotate(
        self, presented_hash: str, new_hash: str, new_expiry: datetime, now: datetime
    ) -> Rotation:
        with self._pool.connection() as conn, conn.transaction():
            reused = conn.execute(
                f"UPDATE sessions SET revoked=true WHERE previous_hash=%s "  # nosec B608 - constant column list, values are bound parameters
                f"RETURNING {self._SESSION}",
                (presented_hash,),
            ).fetchone()
            if reused:
                return Rotation("reuse", _session(reused))
            row = conn.execute(
                f"SELECT {self._SESSION} FROM sessions WHERE refresh_hash=%s FOR UPDATE",  # nosec B608 - constant column list, values are bound parameters
                (presented_hash,),
            ).fetchone()
            if row is None:
                return Rotation("unknown")
            current = _session(row)
            if current.revoked:
                return Rotation("revoked", current)
            if current.expires_at <= now:
                return Rotation("expired", current)
            updated = conn.execute(
                f"UPDATE sessions SET refresh_hash=%s, previous_hash=%s, expires_at=%s "  # nosec B608 - constant column list, values are bound parameters
                f"WHERE session_id=%s RETURNING {self._SESSION}",
                (new_hash, presented_hash, new_expiry, current.session_id),
            ).fetchone()
            return Rotation("ok", _session(updated))

    def revoke_session(self, session_id: str) -> None:
        self._run("UPDATE sessions SET revoked=true WHERE session_id=%s", (session_id,))

    def revoke_all(self, user_id: str) -> None:
        self._run("UPDATE sessions SET revoked=true WHERE user_id=%s", (user_id,))

    def put_profile(self, user_id: str, data: dict[str, Any]) -> None:
        self._run(
            "INSERT INTO profiles (user_id, data) VALUES (%s,%s) "
            "ON CONFLICT (user_id) DO UPDATE SET data=EXCLUDED.data, updated_at=now()",
            (user_id, Jsonb(data)),
        )

    def get_profile(self, user_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT data FROM profiles WHERE user_id=%s", (user_id,))
        return dict(row[0]) if row else None

    def all_profiles(self) -> list[tuple[str, dict[str, Any]]]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT user_id, data FROM profiles").fetchall()
        return [(r[0], dict(r[1])) for r in rows]

    def put_consent(self, user_id: str, version: str, at: datetime) -> None:
        self._run(
            "INSERT INTO consents (user_id, version, accepted_at) VALUES (%s,%s,%s) "
            "ON CONFLICT (user_id) DO UPDATE SET version=EXCLUDED.version, "
            "accepted_at=EXCLUDED.accepted_at",
            (user_id, version, at),
        )

    def consent(self, user_id: str) -> str | None:
        row = self._one("SELECT version FROM consents WHERE user_id=%s", (user_id,))
        return str(row[0]) if row else None

    def audit(self, user_id: str | None, action: str, ip: str, detail: dict[str, Any]) -> None:
        self._run(
            "INSERT INTO audit (user_id, action, ip, detail) VALUES (%s,%s,%s,%s)",
            (user_id, action, ip, Jsonb(detail)),
        )

    def audit_entries(self, user_id: str) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT at, user_id, action, ip, detail FROM audit WHERE user_id=%s ORDER BY id",
                (user_id,),
            ).fetchall()
        return [
            {"at": r[0].isoformat(), "user_id": r[1], "action": r[2], "ip": r[3], "detail": r[4]}
            for r in rows
        ]

    def delete_user(self, user_id: str) -> None:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM audit WHERE user_id=%s", (user_id,))
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))  # cascades the rest

    def add_action_token(
        self, user_id: str, purpose: str, token_hash: str, expires_at: datetime
    ) -> None:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                "DELETE FROM action_tokens WHERE user_id=%s AND purpose=%s", (user_id, purpose)
            )
            conn.execute(
                "INSERT INTO action_tokens (token_hash, user_id, purpose, expires_at) "
                "VALUES (%s,%s,%s,%s)",
                (token_hash, user_id, purpose, expires_at),
            )

    def consume_action_token(self, token_hash: str, purpose: str, now: datetime) -> str | None:
        # one atomic statement: two simultaneous uses cannot both succeed
        row = self._one(
            "UPDATE action_tokens SET used=true WHERE token_hash=%s AND purpose=%s "
            "AND NOT used AND expires_at > %s RETURNING user_id",
            (token_hash, purpose, now),
        )
        return str(row[0]) if row else None

    def mark_email_verified(self, user_id: str) -> None:
        self._run(
            "UPDATE users SET email_verified_at=COALESCE(email_verified_at, now()) "
            "WHERE user_id=%s",
            (user_id,),
        )


__all__ = [
    "AccountStore",
    "EmailTaken",
    "InMemoryAccountStore",
    "PostgresAccountStore",
    "Rotation",
    "Session",
    "User",
]
