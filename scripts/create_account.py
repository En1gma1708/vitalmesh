"""Create an account from the command line (an operator or clinician, or a test user).

    uv run python scripts/create_account.py <email> [--role user|clinician|operator]

The password is read from the VITALMESH_NEW_PASSWORD environment variable or prompted without
echo, never taken as an argument (arguments end up in shell history and process lists). Operators
see the operator routes (tier mix, run internals) and never another person's health data.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from vitalmesh_accounts import (
    ROLES,
    EmailTaken,
    PostgresAccountStore,
    check_email,
    check_password,
    hash_password,
    normalise_email,
)

DB_DSN = os.environ.get("VITALMESH_PG_DSN", "postgresql://vitalmesh@127.0.0.1:5432/vitalmesh")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("email")
    ap.add_argument("--role", choices=ROLES, default="user")
    args = ap.parse_args()
    email = normalise_email(args.email)
    if (problem := check_email(email)) is not None:
        sys.exit(problem)
    password = os.environ.get("VITALMESH_NEW_PASSWORD") or getpass.getpass("Password: ")
    if problems := check_password(password, email):
        sys.exit("; ".join(problems))
    store = PostgresAccountStore(DB_DSN)
    try:
        user = store.create_user(email, hash_password(password), role=args.role)
        store.mark_email_verified(user.user_id)  # created by an operator at the command line
    except EmailTaken:
        sys.exit("that email is already registered")
    finally:
        store.close()
    print(f"created {args.role} {user.user_id} ({email})")


if __name__ == "__main__":
    main()
