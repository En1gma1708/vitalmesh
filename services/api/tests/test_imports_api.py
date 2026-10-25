"""The import flow over HTTP (D79): preview first, nothing stored until the person confirms."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_healthimport import apple, rec, steps
from test_security import World, csrf
from vitalmesh_common.testing import PlainSerde


@pytest.fixture
def w() -> World:
    return World()


WEIGHT = "HKQuantityTypeIdentifierBodyMass"


def day(n: int) -> str:
    return f"2026-10-{n:02d}"


def upload(c: TestClient, data: bytes, name: str = "export.xml", headers: bool = True) -> Any:
    h = {"x-filename": name, "content-type": "application/octet-stream"}
    if headers:
        h.update(csrf(c))
    return c.post("/v1/me/imports", content=data, headers=h)


def sample(w: World) -> bytes:
    # a "now" that makes these days recent: the World clock is real time, so use relative days
    today = w.now.date()
    d1, d2 = (today - timedelta(days=3)).isoformat(), (today - timedelta(days=2)).isoformat()
    return apple(
        steps("4000", d1), steps("6500", d2),
        rec(WEIGHT, "kg", "70.4", f"{d2} 07:00:00 +0000", f"{d2} 07:00:00 +0000"),
    )  # fmt: skip


def test_a_preview_stores_nothing_until_the_person_confirms(w: World) -> None:
    c = w.client()
    w.signup(c)
    r = upload(c, sample(w))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["total"] == 3 and body["source"] == "apple_health" and body["rejected"] == 0
    assert body["summary"]["step_count"]["days"] == 2
    assert w.publisher.sent == []  # nothing reached the event stream yet
    ok = c.post(f"/v1/me/imports/{body['import_id']}/confirm", headers=csrf(c))
    assert ok.status_code == 202 and ok.json() == {"imported": 3}
    assert len(w.publisher.sent) == 3
    decoded = [PlainSerde().deserialize_event(p.topic, p.value) for p in w.publisher.sent]
    kinds = sorted(str(payload.metric_type) for _, payload in decoded)
    assert kinds == ["body_weight", "step_count", "step_count"]
    assert all(str(meta.external_id).startswith("imp:") for meta, _ in decoded)
    assert len({meta.event_id for meta, _ in decoded}) == 3
    assert {meta.user_id for meta, _ in decoded} == {c.get("/v1/me").json()["user_id"]}


def test_the_same_file_twice_gives_the_same_event_ids(w: World) -> None:
    c = w.client()
    w.signup(c)
    ids = []
    for _ in range(2):
        body = upload(c, sample(w)).json()
        c.post(f"/v1/me/imports/{body['import_id']}/confirm", headers=csrf(c))
        ids.append(
            sorted(
                PlainSerde().deserialize_event(p.topic, p.value)[0].event_id
                for p in w.publisher.sent
            )
        )
        w.publisher.sent.clear()
    assert ids[0] == ids[1]  # the processor drops repeats by event id, so nothing doubles


def test_a_confirmation_works_once(w: World) -> None:
    c = w.client()
    w.signup(c)
    body = upload(c, sample(w)).json()
    url = f"/v1/me/imports/{body['import_id']}/confirm"
    assert c.post(url, headers=csrf(c)).status_code == 202
    assert c.post(url, headers=csrf(c)).status_code == 404


def test_a_preview_expires(w: World) -> None:
    c = w.client()
    w.signup(c)
    body = upload(c, sample(w)).json()
    w.mono += 901
    assert c.post(f"/v1/me/imports/{body['import_id']}/confirm", headers=csrf(c)).status_code == 404
    assert w.publisher.sent == []


def test_cancelling_discards_the_preview(w: World) -> None:
    c = w.client()
    w.signup(c)
    body = upload(c, sample(w)).json()
    assert c.delete(f"/v1/me/imports/{body['import_id']}", headers=csrf(c)).status_code == 200
    assert c.post(f"/v1/me/imports/{body['import_id']}/confirm", headers=csrf(c)).status_code == 404


def test_another_person_cannot_confirm_or_cancel_my_import(w: World) -> None:
    mine, theirs = w.client(), w.client()
    w.signup(mine, "a@example.com")
    w.signup(theirs, "b@example.com")
    body = upload(mine, sample(w)).json()
    url = f"/v1/me/imports/{body['import_id']}"
    assert theirs.post(url + "/confirm", headers=csrf(theirs)).status_code == 404
    theirs.delete(url, headers=csrf(theirs))
    assert w.publisher.sent == []
    assert mine.post(url + "/confirm", headers=csrf(mine)).status_code == 202  # still there


def test_an_import_needs_a_session_and_the_csrf_header(w: World) -> None:
    anon = w.client()
    assert upload(anon, sample(w), headers=False).status_code == 401
    c = w.client()
    w.signup(c)
    assert upload(c, sample(w), headers=False).status_code == 403
    assert c.post("/v1/me/imports/abcdefgh1234/confirm").status_code == 403


def test_implausible_values_are_rejected_not_clamped(w: World) -> None:
    c = w.client()
    w.signup(c)
    d = (w.now.date() - timedelta(days=2)).isoformat()
    data = apple(
        rec(WEIGHT, "kg", "900", f"{d} 07:00:00 +0000", f"{d} 07:00:00 +0000"),
        steps("5000", d),
    )
    body = upload(c, data).json()
    assert body["total"] == 1 and body["rejected"] == 1
    assert "body_weight" not in body["summary"]


def test_an_import_of_nothing_usable_is_refused_with_a_reason(w: World) -> None:
    c = w.client()
    w.signup(c)
    r = upload(c, apple(steps("5000", "2020-01-01")))
    assert r.status_code == 422 and "no usable readings" in r.json()["detail"]


def test_hostile_and_oversized_files_are_refused_over_http(w: World) -> None:
    c = w.client()
    w.signup(c)
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE HealthData [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
        b'<HealthData><Record type="HKQuantityTypeIdentifierStepCount" value="&b;"/></HealthData>'
    )
    r = upload(c, bomb)
    assert r.status_code == 422 and "XML features" in r.json()["detail"]
    big = upload(c, bytes(50 * 1024 * 1024 + 1))
    assert big.status_code == 413


def test_uploads_are_limited_per_person(w: World) -> None:
    c = w.client()
    w.signup(c)
    codes = [upload(c, sample(w)).status_code for _ in range(6)]
    assert codes[:5] == [201] * 5 and codes[5] == 429
