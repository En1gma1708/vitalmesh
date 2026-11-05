"""Recent questions and answers (D83): own data only, in the export, gone on erase."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from vitalmesh_accounts import (
    InMemoryAccountStore,
    InMemoryQaHistory,
    InMemoryRateLimiter,
    OutboxMailer,
    TokenIssuer,
    qa_export,
)
from vitalmesh_api.app import create_app
from vitalmesh_api.googlecal import TokenBox
from vitalmesh_api.security import CSRF_COOKIE, Auth
from vitalmesh_common.testing import PlainSerde, RecordingPublisher
from vitalmesh_stores import InMemoryPendingImports, InMemoryRunLog
from vitalmesh_t0.profiles import InMemoryProfileStore

PASSWORD = "correct horse battery"
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
ANSWER: dict[str, Any] = {
    "text": "Aim for about 2359 kcal.",
    "partial": False,
    "missing": [],
    "claims": [{"text": "2359 kcal per day", "basis": "computed"}],
    "citations": [],
}


class World:
    def __init__(self) -> None:
        self.qa = InMemoryQaHistory()
        self.store = InMemoryAccountStore()
        self.auth = Auth(
            self.store,
            TokenIssuer(b"s" * 32),
            InMemoryRateLimiter(),
            InMemoryProfileStore(),
            purge=self.qa.purge,
            export_extra=lambda uid: {"questions": qa_export(self.qa, uid)},
            mailer=OutboxMailer(),
            require_verified=False,
        )
        self.app = create_app(
            InMemoryProfileStore(), PlainSerde(), RecordingPublisher(), auth=self.auth,
            runs=InMemoryRunLog(), imports=InMemoryPendingImports(),
            token_box=TokenBox(Fernet.generate_key()), qa=self.qa,
            ops=lambda: {"tiers": {}, "cohort": {}},
        )  # fmt: skip

    def person(self, email: str) -> tuple[TestClient, str]:
        c = TestClient(self.app)
        r = c.post("/v1/auth/signup", json={"email": email, "password": PASSWORD})
        assert r.status_code == 201, r.text
        return c, str(r.json()["user_id"])


def csrf(c: TestClient) -> dict[str, str]:
    return {"x-csrf-token": c.cookies.get(CSRF_COOKIE) or ""}


def test_a_person_sees_their_questions_newest_first_with_the_answer_shown() -> None:
    w = World()
    c, uid = w.person("a@example.com")
    w.qa.record_question(uid, "c1", "first question", NOW)
    w.qa.record_question(uid, "c2", "second question", NOW + timedelta(minutes=1))
    w.qa.record_answer(uid, "c2", "released", ANSWER, NOW + timedelta(minutes=1))
    got = c.get(f"/v1/users/{uid}/history").json()
    assert [g["question"] for g in got] == ["second question", "first question"]
    assert got[0]["status"] == "released"
    assert got[0]["answer"]["text"] == ANSWER["text"]
    assert got[0]["answer"]["items"][0]["basis"] == "Calculated from your profile and recent data"
    assert got[1]["answer"] is None  # still waiting: no invented answer


def test_nobody_reads_another_persons_history() -> None:
    w = World()
    _, a_id = w.person("a@example.com")
    b, _ = w.person("b@example.com")
    w.qa.record_question(a_id, "c1", "private question", NOW)
    assert b.get(f"/v1/users/{a_id}/history").status_code == 404
    assert TestClient(w.app).get(f"/v1/users/{a_id}/history").status_code == 401
    assert "private question" not in b.get(f"/v1/users/{a_id}/history").text


def test_the_limit_is_bounded() -> None:
    w = World()
    c, uid = w.person("a@example.com")
    for i in range(12):
        w.qa.record_question(uid, f"c{i}", f"q{i}", NOW + timedelta(minutes=i))
    assert len(c.get(f"/v1/users/{uid}/history").json()) == 10
    assert len(c.get(f"/v1/users/{uid}/history?limit=3").json()) == 3
    assert len(c.get(f"/v1/users/{uid}/history?limit=100000").json()) == 12
    assert len(c.get(f"/v1/users/{uid}/history?limit=-5").json()) == 1


def test_history_is_in_the_export_and_gone_after_the_account_is_deleted() -> None:
    w = World()
    c, uid = w.person("a@example.com")
    w.qa.record_question(uid, "c1", "what should I eat", NOW)
    w.qa.record_answer(uid, "c1", "released", ANSWER, NOW)
    exported = c.get("/v1/me/export").json()["data"]["questions"]
    assert exported[0]["question"] == "what should I eat" and exported[0]["answer"] == ANSWER
    r = c.request("DELETE", "/v1/me", json={"password": PASSWORD}, headers=csrf(c))
    assert r.status_code == 204 or r.status_code == 200
    assert w.qa.recent(uid) == []
