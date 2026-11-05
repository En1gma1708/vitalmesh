from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from vitalmesh_accounts import PostgresReviewQueue

sys.path.insert(0, str(Path(__file__).parent.parent))
from review_contract import CHECKS

pytestmark = pytest.mark.integration

ADMIN_DSN = "postgresql://vitalmesh@127.0.0.1:5432/vitalmesh"
TEST_DSN = "postgresql://vitalmesh@127.0.0.1:5432/vitalmesh_test"


@pytest.fixture
def queue() -> Iterator[PostgresReviewQueue]:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname='vitalmesh_test'").fetchone():
            admin.execute("CREATE DATABASE vitalmesh_test")
    q = PostgresReviewQueue(TEST_DSN)
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute("TRUNCATE reviews")
    yield q
    q.close()


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
def test_postgres_review_queue_meets_the_same_contract(queue, check) -> None:  # type: ignore[no-untyped-def]
    check(queue)
