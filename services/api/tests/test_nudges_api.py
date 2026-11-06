"""The person's side of nudges (D87): the switch, the inbox, dismissing. Own data only."""

from __future__ import annotations

from datetime import timedelta

import pytest
from test_security import World, csrf
from vitalmesh_stores import Item


@pytest.fixture
def w() -> World:
    return World()


def test_nudges_are_off_until_the_person_turns_them_on(w: World) -> None:
    c = w.client()
    w.signup(c)
    assert c.get("/v1/me/nudges").json() == {"enabled": False, "timezone": "UTC"}
    r = c.put("/v1/me/nudges", json={"enabled": True, "timezone": "Asia/Kolkata"}, headers=csrf(c))
    assert r.status_code == 200 and r.json() == {"enabled": True, "timezone": "Asia/Kolkata"}
    assert c.get("/v1/me/nudges").json()["enabled"] is True
    uid = w.store.by_email("a@example.com").user_id  # type: ignore[union-attr]
    assert w.nudges.get_prefs(uid).timezone == "Asia/Kolkata"
    assert any(e["action"] == "nudges_changed" for e in w.store.audit_entries(uid))  # audited


@pytest.mark.parametrize("zone", ["Mars/Base", "../../etc/passwd", "", "x" * 200, "UTC; drop"])
def test_a_bad_timezone_is_refused(w: World, zone: str) -> None:
    c = w.client()
    w.signup(c)
    r = c.put("/v1/me/nudges", json={"enabled": True, "timezone": zone}, headers=csrf(c))
    assert r.status_code == 422
    assert c.get("/v1/me/nudges").json()["enabled"] is False


def test_extra_fields_are_refused_and_changes_need_the_csrf_token(w: World) -> None:
    c = w.client()
    w.signup(c)
    assert (
        c.put(
            "/v1/me/nudges", json={"enabled": True, "user_id": "u-x"}, headers=csrf(c)
        ).status_code
        == 422
    )
    assert c.put("/v1/me/nudges", json={"enabled": True}).status_code == 403


def test_the_inbox_shows_only_the_persons_own_undismissed_nudges_newest_first(w: World) -> None:
    a, b = w.client(), w.client()
    ua = w.signup(a)
    w.signup(b, email="b@example.org")
    for n, (kind, hours) in enumerate(
        [("log_reminder", 30), ("activity_behind", 5), ("log_reminder", 50)]
    ):
        w.nudges.add(
            ua, Item(f"nudge-{n}-aaaa", kind, "gentle", f"text {n}", w.now - timedelta(hours=hours))
        )
    got = a.get("/v1/me/notifications").json()
    assert [i["id"] for i in got] == ["nudge-1-aaaa", "nudge-0-aaaa", "nudge-2-aaaa"]
    assert b.get("/v1/me/notifications").json() == []
    assert a.post("/v1/me/notifications/nudge-0-aaaa/dismiss", headers=csrf(a)).status_code == 200
    assert [i["id"] for i in a.get("/v1/me/notifications").json()] == [
        "nudge-1-aaaa",
        "nudge-2-aaaa",
    ]


def test_nobody_can_dismiss_someone_elses_nudge(w: World) -> None:
    a, b = w.client(), w.client()
    ua = w.signup(a)
    w.signup(b, email="b@example.org")
    w.nudges.add(ua, Item("nudge-9-aaaa", "log_reminder", "gentle", "t", w.now))
    assert b.post("/v1/me/notifications/nudge-9-aaaa/dismiss", headers=csrf(b)).status_code == 404
    assert [i["id"] for i in a.get("/v1/me/notifications").json()] == ["nudge-9-aaaa"]
    for bad in ("x", "a" * 80, "nudge;drop"):
        assert b.post(f"/v1/me/notifications/{bad}/dismiss", headers=csrf(b)).status_code in (
            404,
            422,
        )


def test_everything_here_needs_a_login(w: World) -> None:
    c = w.client()
    assert c.get("/v1/me/nudges").status_code == 401
    assert c.put("/v1/me/nudges", json={"enabled": True}).status_code in (401, 403)
    assert c.get("/v1/me/notifications").status_code == 401
    assert c.post("/v1/me/notifications/nudge-0-aaaa/dismiss").status_code in (401, 403)
