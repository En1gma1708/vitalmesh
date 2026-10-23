from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from vitalmesh_accounts import PostgresAccountStore

sys.path.insert(0, str(Path(__file__).parent.parent))
from contract import CHECKS

pytestmark = pytest.mark.integration

ADMIN_DSN = "postgresql://vitalmesh@127.0.0.1:5432/vitalmesh"
TEST_DSN = "postgresql://vitalmesh@127.0.0.1:5432/vitalmesh_test"


@pytest.fixture
def store() -> Iterator[PostgresAccountStore]:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        found = admin.execute("SELECT 1 FROM pg_database WHERE datname='vitalmesh_test'")
        if not found.fetchone():
            admin.execute("CREATE DATABASE vitalmesh_test")  # never touches the real corpus
    s = PostgresAccountStore(TEST_DSN)
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute("TRUNCATE users, audit CASCADE")
    yield s
    s.close()


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
def test_postgres_store_meets_the_same_contract(store, check) -> None:  # type: ignore[no-untyped-def]
    check(store)
