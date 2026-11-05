from __future__ import annotations

import pytest
from review_contract import CHECKS
from vitalmesh_accounts import InMemoryReviewQueue


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
def test_in_memory_review_queue_meets_the_contract(check) -> None:  # type: ignore[no-untyped-def]
    check(InMemoryReviewQueue())
