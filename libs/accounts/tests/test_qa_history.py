from __future__ import annotations

import pytest
from history_contract import CHECKS
from vitalmesh_accounts import InMemoryQaHistory


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
def test_in_memory_history_meets_the_contract(check) -> None:  # type: ignore[no-untyped-def]
    check(InMemoryQaHistory())
