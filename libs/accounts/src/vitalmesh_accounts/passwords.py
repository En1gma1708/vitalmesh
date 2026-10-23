"""Passwords: argon2id hashing and the sign-up policy (D75).

* Hashing is argon2id with the library's current defaults; the hash string carries its own
  parameters, so raising the cost later re-hashes users on their next login (``needs_rehash``).
* Policy follows the current NIST guidance (SP 800-63B): length, not composition rules, and a
  blocklist of passwords that are known to be guessed first. 10 to 128 characters; not the email
  or its local part; not on the blocklist.
* ``verify`` against an unknown account still does a full hash, so response time does not reveal
  which emails are registered.
"""

from __future__ import annotations

import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_LENGTH = 10
MAX_LENGTH = 128
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")

# A short blocklist of the most guessed passwords that pass a length check. A real deployment
# would load a large list; the policy and the place it plugs in are what matter here.
COMMON = frozenset(
    {
        "password123", "1234567890", "qwertyuiop", "password1234", "iloveyou123", "welcome123",
        "letmein1234", "admin12345", "abcdefghij", "0123456789", "1q2w3e4r5t", "passw0rd123",
        "changeme123", "qwerty12345", "football123", "monkey12345", "dragon12345", "baseball123",
    }
)  # fmt: skip

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("not-a-real-password-for-timing")


def normalise_email(email: str) -> str:
    return email.strip().lower()


def check_email(email: str) -> str | None:
    """None if acceptable, else a plain reason."""
    return None if _EMAIL.match(normalise_email(email)) else "enter a valid email address"


def check_password(password: str, email: str) -> list[str]:
    """Plain-language reasons the password is not acceptable; empty if it is."""
    problems: list[str] = []
    if len(password) < MIN_LENGTH:
        problems.append(f"use at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        problems.append(f"use at most {MAX_LENGTH} characters")
    low = password.lower()
    local = normalise_email(email).split("@")[0]
    if low in COMMON:
        problems.append("that password is too common")
    if local and (low == normalise_email(email) or (len(local) >= 4 and local in low)):
        problems.append("do not use your email in your password")
    return problems


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """True only for a match. A missing hash (unknown account) still spends the hashing time."""
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)
