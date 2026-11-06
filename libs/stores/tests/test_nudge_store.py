from __future__ import annotations

from nudge_contract import check_contract
from vitalmesh_stores import InMemoryNudgeStore


def test_the_in_memory_nudge_store_meets_the_contract() -> None:
    check_contract(InMemoryNudgeStore(), "u-1", "u-2")
