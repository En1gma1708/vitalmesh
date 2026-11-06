"""One contract, run against the in-memory twin and Redis (D87)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vitalmesh_stores import Item, NudgeStore, Prefs

T0 = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


def check_contract(store: NudgeStore, user: str, other: str) -> None:
    assert store.get_prefs(user) == Prefs(enabled=False, timezone="UTC")  # off until opted in
    store.set_prefs(user, Prefs(enabled=True, timezone="Asia/Kolkata"))
    assert store.get_prefs(user) == Prefs(enabled=True, timezone="Asia/Kolkata")
    assert store.get_prefs(other).enabled is False  # one person's switch is not another's

    first = Item("n-1", "log_reminder", "gentle", "text one", T0)
    assert store.add(user, first) is True
    assert store.add(user, first) is False  # a redelivered event adds nothing
    store.add(user, Item("n-2", "activity_behind", "direct", "text two", T0 + timedelta(hours=1)))
    assert [i.id for i in store.items(user)] == ["n-2", "n-1"]  # newest first
    assert store.items(other) == []

    assert store.dismiss(user, "n-1") is True
    assert store.dismiss(user, "nope") is False
    assert store.dismiss(other, "n-2") is False  # cannot dismiss someone else's item
    by_id = {i.id: i for i in store.items(user)}
    assert by_id["n-1"].dismissed is True and by_id["n-2"].dismissed is False

    for n in range(60):  # the inbox is bounded; the oldest fall away
        store.add(
            user,
            Item(f"bulk-{n}", "log_reminder", "gentle", "t", T0 + timedelta(days=1, minutes=n)),
        )
    got = store.items(user)
    assert len(got) == 50 and got[0].id == "bulk-59" and "n-1" not in {i.id for i in got}

    before = store.stats().get("x_test", 0)
    store.stat("x_test")
    store.stat("x_test", 4)
    assert store.stats()["x_test"] == before + 5

    assert store.decision("k") is None
    store.remember_decision("k", "send|log_reminder|gentle")
    assert store.decision("k") == "send|log_reminder|gentle"
