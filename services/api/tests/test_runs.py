from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from vitalmesh_api.app import create_app
from vitalmesh_api.runview import BLOCKED_TEXT, view_of
from vitalmesh_common.testing import PlainSerde, RecordingPublisher
from vitalmesh_stores import InMemoryRunLog
from vitalmesh_t0.profiles import InMemoryProfileStore

T = "2026-10-23T10:00:00+00:00"


def e(type_: str, **extra: Any) -> dict[str, Any]:
    return {"event_id": f"{type_}-{len(extra)}", "at": T, "type": type_, "tier": None, **extra}


RELEASED_RUN = [
    e("question.asked", question="What should I eat?"),
    e(
        "run.planned",
        tier="T3",
        specialists=["nutrition", "condition_medication"],
        rules=["route.nutrition_terms"],
        risk="low",
        degraded=False,
    ),
    e("advice.completed", specialist="nutrition", status="answered", model_calls=2, failure=""),
    e("advice.bundle.ready", partial=False, missing=[], reason="complete"),
    e("recommendation.drafted", revision=0, used_fallback=False),
    e("safety.cleared", rules_version="3+t3-1"),
    e(
        "recommendation.released",
        text="Aim for 2100 kcal.",
        partial=False,
        missing=[],
        claims=[{"text": "Aim for about 2100 kcal.", "basis": "computed"}],
        citations=[{"document": "who", "locator": "p. 3", "licence": "CC BY", "url": "https://x"}],
    ),
]


def client(entries: list[dict[str, Any]], owner: str = "u-1") -> TestClient:
    log = InMemoryRunLog()
    for entry in entries:
        log.append("run-1", owner, entry)
    app = create_app(
        InMemoryProfileStore(),
        PlainSerde(),
        RecordingPublisher(),
        runs=log,
        ops=lambda: {"tiers": {}, "cohort": {}},
        stream_poll_s=0.01,
        stream_max_s=0.2,
    )
    return TestClient(app)


def test_the_user_view_is_plain_language_without_internals() -> None:
    view = view_of(RELEASED_RUN)
    text = " ".join(s.text for s in view.steps)
    assert view.status == "released" and view.answer is not None
    assert "Food" in text and "Your conditions and labs" in text and "Safety check passed" in text
    for internal in ("route.nutrition_terms", "T3", "nutrition", "model", "3+t3-1", "rule"):
        assert internal not in text
    assert view.answer.items[0].basis == "Calculated from your profile and recent data"
    assert view.answer.sources[0].licence == "CC BY"


def test_run_route_returns_the_view_to_its_owner_only() -> None:
    c = client(RELEASED_RUN)
    assert c.get("/v1/users/u-1/runs/run-1").json()["status"] == "released"
    assert c.get("/v1/users/u-2/runs/run-1").status_code == 404  # another user's run
    assert c.get("/v1/users/u-1/runs/nope").status_code == 404


def test_a_blocked_run_tells_the_user_generically() -> None:
    entries = [
        e("question.asked", question="q"),
        e("recommendation.blocked", rules=["numbers_grounded"]),
    ]
    view = view_of(entries)
    assert view.status == "blocked" and view.answer is None
    assert "numbers_grounded" not in " ".join(s.text for s in view.steps)
    assert BLOCKED_TEXT  # the app shows this line; the reason stays internal (D40)


def test_a_run_waiting_for_a_clinician_says_so() -> None:
    entries = [e("question.asked", question="q"), e("review.requested", reasons=["risk.high"])]
    view = view_of(entries)
    assert view.status == "needs_review" and "clinician" in view.steps[-1].text


def test_stream_sends_each_step_then_done() -> None:
    body = client(RELEASED_RUN).get("/v1/users/u-1/runs/run-1/stream").text
    assert body.count("event: step") == len(view_of(RELEASED_RUN).steps)
    assert body.rstrip().endswith("}") and "event: done" in body


def test_stream_of_an_unfinished_run_times_out_politely() -> None:
    body = client(RELEASED_RUN[:3]).get("/v1/users/u-1/runs/run-1/stream").text
    assert "event: timeout" in body and "event: done" not in body


def test_ops_view_has_the_internals() -> None:
    body = client(RELEASED_RUN).get("/v1/ops/runs/run-1").json()
    assert any(en.get("rules") == ["route.nutrition_terms"] for en in body["entries"])
