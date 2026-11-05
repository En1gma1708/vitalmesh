"""The clinician review console (D85): who may reach it, what it shows, and what a decision does."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from vitalmesh_accounts import (
    APPROVED,
    PENDING,
    REJECTED,
    InMemoryAccountStore,
    InMemoryRateLimiter,
    InMemoryReviewQueue,
    OutboxMailer,
    ReviewCase,
    TokenIssuer,
    hash_password,
    review_export,
)
from vitalmesh_api.app import create_app
from vitalmesh_api.googlecal import TokenBox
from vitalmesh_api.security import CSRF_COOKIE, Auth
from vitalmesh_common import codec
from vitalmesh_common.testing import PlainSerde, RecordingPublisher
from vitalmesh_common.topics import REVIEWS_DECIDED
from vitalmesh_stores import InMemoryPendingImports, InMemoryRunLog
from vitalmesh_t0.profiles import InMemoryProfileStore

PASSWORD = "correct horse battery"
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
SECRET_Q = "do my results mean I have a thyroid problem"
SECRET_DRAFT = "Your TSH is flagged; please see a doctor soon."
OWNER = "u-owner-123"


def held(rec: str = "rec-1", minutes: int = 0) -> ReviewCase:
    return ReviewCase(
        recommendation_id=rec,
        correlation_id=f"corr-{rec}",
        requested_at=NOW + timedelta(minutes=minutes),
        question=SECRET_Q,
        briefing="A marked lab value needs a look.",
        reasons=["risk.high", "lab.marked"],
        draft={
            "text": SECRET_DRAFT,
            "risk": "high",
            "partial": False,
            "claims": [{"text": "TSH 9.1 mIU/L", "basis": "computed"}],
            "citations": [{"document": "ata-2012", "locator": "p. 4"}],
        },
    )


class World:
    def __init__(self) -> None:
        self.reviews = InMemoryReviewQueue()
        self.publisher = RecordingPublisher()
        self.store = InMemoryAccountStore()
        self.auth = Auth(
            self.store,
            TokenIssuer(b"s" * 32),
            InMemoryRateLimiter(),
            InMemoryProfileStore(),
            purge=self.reviews.purge,
            export_extra=lambda uid: {"held_answers": review_export(self.reviews, uid)},
            mailer=OutboxMailer(),
            require_verified=False,
        )
        self.app = create_app(
            InMemoryProfileStore(), PlainSerde(), self.publisher, auth=self.auth,
            runs=InMemoryRunLog(), imports=InMemoryPendingImports(),
            token_box=TokenBox(Fernet.generate_key()), reviews=self.reviews, clock=lambda: NOW,
            ops=lambda: {"tiers": {}, "cohort": {}},
        )  # fmt: skip
        self.reviews.put_request(OWNER, held())

    def signed_in(self, email: str, role: str) -> tuple[TestClient, str]:
        user = self.store.create_user(email, hash_password(PASSWORD), role=role)
        c = TestClient(self.app)
        r = c.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        return c, user.user_id


def csrf(c: TestClient) -> dict[str, str]:
    return {"x-csrf-token": c.cookies.get(CSRF_COOKIE) or ""}


def decide(c: TestClient, rec: str = "rec-1", decision: str = "approve", **body: Any) -> Any:
    return c.post(
        f"/v1/clinician/reviews/{rec}/decision",
        json={"decision": decision, **body},
        headers=csrf(c),
    )


def test_only_a_clinician_reaches_the_review_routes() -> None:
    w = World()
    paths = ["/v1/clinician/reviews", "/v1/clinician/reviews/rec-1"]
    anon = TestClient(w.app)
    user, _ = w.signed_in("user@example.com", "user")
    operator, _ = w.signed_in("ops@example.com", "operator")
    clinician, _ = w.signed_in("doc@example.com", "clinician")
    for path in paths:
        assert anon.get(path).status_code == 401, path
        assert user.get(path).status_code == 403, path
        assert operator.get(path).status_code == 403, path  # operators see flow, never content
        assert clinician.get(path).status_code == 200, path
    for c, code in ((anon, 401), (user, 403), (operator, 403)):
        r = c.post("/v1/clinician/reviews/rec-1/decision", json={"decision": "approve"})
        assert r.status_code in (401, 403) and (c is anon) == (r.status_code == 401)
        assert decide(c).status_code == code
    assert w.publisher.sent == []  # nothing got through
    assert w.reviews.get("rec-1").status == PENDING  # type: ignore[union-attr]


def test_the_queue_lists_cases_without_content_and_never_the_person() -> None:
    w = World()
    c, _ = w.signed_in("doc@example.com", "clinician")
    body = c.get("/v1/clinician/reviews")
    assert body.status_code == 200
    assert (
        body.json()[0]["reasons"] == ["risk.high", "lab.marked"]
        and body.json()[0]["risk"] == "high"
    )
    for secret in (SECRET_Q, SECRET_DRAFT, OWNER):
        assert secret not in body.text


def test_opening_a_case_shows_the_content_but_not_the_person_and_is_audited() -> None:
    w = World()
    c, doc = w.signed_in("doc@example.com", "clinician")
    r = c.get("/v1/clinician/reviews/rec-1")
    d = r.json()
    assert d["question"] == SECRET_Q and d["draft_text"] == SECRET_DRAFT
    assert d["claims"] == [{"text": "TSH 9.1 mIU/L", "basis": "computed"}]
    assert d["sources"] == [{"document": "ata-2012", "locator": "p. 4"}]
    assert OWNER not in r.text
    viewed = [e for e in w.store.audit_entries(doc) if e["action"] == "review_viewed"]
    assert len(viewed) == 1 and viewed[0]["detail"] == {"recommendation_id": "rec-1"}
    assert c.get("/v1/clinician/reviews/nope").status_code == 404


def test_approving_publishes_one_decision_event_on_the_runs_own_correlation_id() -> None:
    w = World()
    c, doc = w.signed_in("doc@example.com", "clinician")
    r = decide(c, "rec-1", "approve", note="reads fine")
    assert r.status_code == 200 and r.json() == {"recommendation_id": "rec-1", "status": APPROVED}
    [sent] = w.publisher.sent
    assert sent.topic == REVIEWS_DECIDED and sent.key == OWNER  # the person's partition
    meta, msg = codec.decode(sent.value)
    assert (meta.type, meta.correlation_id) == ("review.decided", "corr-rec-1")
    assert (msg.recommendation_id, msg.reviewer_id, msg.note) == ("rec-1", doc, "reads fine")
    assert msg.decision == 1  # DECISION_APPROVE
    case = w.reviews.get("rec-1")
    assert case is not None and case.status == APPROVED and case.decided_by == doc
    audit = [e for e in w.store.audit_entries(doc) if e["action"] == "review_decided"]
    assert audit[0]["detail"] == {"recommendation_id": "rec-1", "decision": "approve"}
    assert "reads fine" not in str(w.store.audit_entries(doc))  # the audit never holds the note


def test_rejecting_publishes_a_reject_and_a_second_decision_is_refused() -> None:
    w = World()
    c, _ = w.signed_in("doc@example.com", "clinician")
    assert decide(c, "rec-1", "reject").json()["status"] == REJECTED
    again = decide(c, "rec-1", "approve")
    assert again.status_code == 409
    other, _ = w.signed_in("doc2@example.com", "clinician")
    assert decide(other, "rec-1", "approve").status_code == 409  # another clinician, same
    assert len(w.publisher.sent) == 1  # only the first reached the pipeline
    _, msg = codec.decode(w.publisher.sent[0].value)
    assert msg.decision == 2  # DECISION_REJECT
    assert [
        x["recommendation_id"] for x in c.get("/v1/clinician/reviews?status=decided").json()
    ] == ["rec-1"]
    assert c.get("/v1/clinician/reviews").json() == []


def test_bad_input_is_refused_before_anything_is_published() -> None:
    w = World()
    c, _ = w.signed_in("doc@example.com", "clinician")
    assert decide(c, "rec-1", "maybe").status_code == 422
    assert decide(c, "rec-1", "approve", note="x" * 501).status_code == 422
    assert (
        c.post(
            "/v1/clinician/reviews/rec-1/decision",
            json={"decision": "approve", "extra": 1},
            headers=csrf(c),
        ).status_code
        == 422
    )
    assert decide(c, "no-such", "approve").status_code == 404
    assert c.get("/v1/clinician/reviews/bad id!").status_code in (404, 422)
    assert w.publisher.sent == []
    assert w.reviews.get("rec-1").status == PENDING  # type: ignore[union-attr]


def test_a_decision_without_the_csrf_token_is_refused() -> None:
    w = World()
    c, _ = w.signed_in("doc@example.com", "clinician")
    r = c.post("/v1/clinician/reviews/rec-1/decision", json={"decision": "approve"})
    assert r.status_code == 403
    assert w.publisher.sent == []


def test_a_broker_outage_decides_nothing() -> None:
    w = World()
    c, _ = w.signed_in("doc@example.com", "clinician")
    w.publisher.fail_flush = True
    assert decide(c, "rec-1", "approve").status_code == 503
    assert w.reviews.get("rec-1").status == PENDING  # type: ignore[union-attr]
    w.publisher.fail_flush = False
    assert decide(c, "rec-1", "approve").status_code == 200  # the clinician can simply retry


def test_a_held_answer_is_in_the_owners_export_and_gone_when_they_leave() -> None:
    w = World()
    user = w.store.create_user("owner@example.com", hash_password(PASSWORD))
    w.reviews.put_request(user.user_id, held("rec-mine"))
    c = TestClient(w.app)
    assert (
        c.post(
            "/v1/auth/login", json={"email": "owner@example.com", "password": PASSWORD}
        ).status_code
        == 200
    )
    mine = c.get("/v1/me/export").json()["data"]["held_answers"]
    assert [m["question"] for m in mine] == [SECRET_Q] and mine[0]["status"] == PENDING
    r = c.request("DELETE", "/v1/me", json={"password": PASSWORD}, headers=csrf(c))
    assert r.status_code in (200, 204)
    assert w.reviews.get("rec-mine") is None and w.reviews.get("rec-1") is not None


def test_the_page_is_served_with_a_nonce() -> None:
    r = TestClient(World().app).get("/clinician")
    assert r.status_code == 200 and "Clinician review" in r.text
    assert "<script>" not in r.text and 'nonce="' in r.text
