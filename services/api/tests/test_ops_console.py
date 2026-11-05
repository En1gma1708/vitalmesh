"""The engineering console (D84): timing and flow are measured, content is never shown."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from vitalmesh_api.app import create_app
from vitalmesh_api.opsview import eval_rows, percentiles, redact, summarise_run
from vitalmesh_common.lag import LagRow, lag_of
from vitalmesh_common.testing import PlainSerde, RecordingPublisher
from vitalmesh_stores import InMemoryRunLog
from vitalmesh_t0.profiles import InMemoryProfileStore

T0 = datetime(2026, 11, 6, 10, 0, 0, tzinfo=UTC)
QUESTION = "what is my secret diagnosis"
ANSWER = "Aim for about 2100 kcal."


def ev(i: int, ms: int, type_: str, **extra: Any) -> dict[str, Any]:
    at = (T0 + timedelta(milliseconds=ms)).isoformat()
    return {"event_id": f"e{i}-{type_}", "at": at, "type": type_, "tier": None, **extra}


def run(total_ms: int = 4000, tier: str = "T3") -> list[dict[str, Any]]:
    return [
        ev(0, 0, "question.asked", question=QUESTION),
        ev(1, 150, "run.planned", tier=tier, specialists=["nutrition"], risk="low", rules=["r"]),
        ev(2, 2500, "advice.completed", specialist="nutrition", status="answered", model_calls=2),
        ev(3, 3000, "recommendation.drafted", revision=0, used_fallback=False),
        ev(
            4,
            total_ms,
            "recommendation.released",
            text=ANSWER,
            partial=False,
            missing=[],
            claims=[{"text": "2100 kcal", "basis": "computed"}],
            citations=[{"document": "d", "locator": "l", "licence": "x", "url": "https://x"}],
        ),
    ]


def test_a_run_is_summarised_from_its_event_times() -> None:
    s = summarise_run("c1", run())
    assert (s.status, s.tier, s.specialists, s.risk) == ("released", "T3", ["nutrition"], "low")
    assert s.total_ms == 4000 and s.model_calls == 2 and not s.partial
    assert [(h.type, h.at_ms) for h in s.hops][:2] == [("question.asked", 0), ("run.planned", 150)]


def test_a_running_run_has_no_total_yet() -> None:
    s = summarise_run("c1", run()[:3])
    assert s.status == "running" and s.total_ms is None


def test_percentiles_are_nearest_rank_and_say_how_many_runs() -> None:
    assert percentiles([]).runs == 0 and percentiles([]).p95_ms is None
    p = percentiles(list(range(100, 2100, 100)))  # 20 runs: 100 .. 2000
    assert (p.runs, p.p50_ms, p.p95_ms) == (20, 1000, 1900)
    one = percentiles([700])
    assert (one.p50_ms, one.p95_ms) == (700, 700)  # with one run both are that run


def test_the_operator_copy_has_no_question_answer_claims_or_problems() -> None:
    entries = [
        *run(),
        ev(5, 4100, "review.completed", verdict="approve", problems=["quote of the answer"]),
    ]
    shown = json.dumps(redact(entries))
    for secret in (QUESTION, ANSWER, "2100 kcal", "quote of the answer", "https://x"):
        assert secret not in shown
    released = next(e for e in redact(entries) if e["type"] == "recommendation.released")
    assert released["claims_n"] == 1 and released["citations_n"] == 1
    assert next(e for e in redact(entries) if e["type"] == "run.planned")["rules"] == ["r"]


def test_eval_rows_count_passes_and_skip_unreadable_files(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text(json.dumps({"cases": {"x": True, "y": False, "z": True}}))
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "empty.json").write_text(json.dumps({"cases": {}}))
    rows = eval_rows(tmp_path)
    assert [(r.name, r.passed, r.total) for r in rows] == [("a", 2, 3)]
    assert eval_rows(tmp_path / "missing") == []


def test_lag_is_the_unprocessed_messages() -> None:
    assert lag_of(committed=7, low=0, high=10) == 3
    assert lag_of(committed=None, low=0, high=10) == 10  # never committed: everything is waiting
    assert lag_of(committed=None, low=4, high=10) == 6  # older messages expired
    assert lag_of(committed=2, low=5, high=10) == 5  # a commit older than retention counts from low
    assert lag_of(committed=10, low=0, high=10) == 0
    assert lag_of(committed=12, low=0, high=10) == 0  # never negative


def make(
    entries_by_run: dict[str, list[dict[str, Any]]] | None = None,
    lag: Any = None,
    evals: Any = None,
    graph: Any = None,
    ops: Any = lambda: {"tiers": {}, "cohort": {}},
) -> TestClient:
    log = InMemoryRunLog()
    for cid, entries in (entries_by_run or {}).items():
        for entry in entries:
            log.append(cid, "u-1", entry)
    app = create_app(
        InMemoryProfileStore(),
        PlainSerde(),
        RecordingPublisher(),
        runs=log,
        ops=ops,
        lag=lag,
        evals=evals,
        graph=graph,
    )
    return TestClient(app)


def test_runs_list_has_newest_first_and_measured_percentiles_by_tier() -> None:
    c = make({"old": run(3000, "T3"), "mid": run(5000, "T3"), "new": run(800, "T1")})
    body = c.get("/v1/ops/runs").json()
    assert [r["correlation_id"] for r in body["runs"]] == ["new", "mid", "old"]
    assert body["latency"]["runs"] == 3 and body["latency"]["p50_ms"] == 3000
    assert body["latency_by_tier"]["T1"] == {"runs": 1, "p50_ms": 800, "p95_ms": 800}
    assert body["latency_by_tier"]["T3"]["runs"] == 2


def test_the_console_routes_never_carry_the_question_or_the_answer() -> None:
    c = make({"r1": run()})
    for path in ("/v1/ops/runs", "/v1/ops/runs/r1"):
        text = c.get(path).text
        assert QUESTION not in text and ANSWER not in text, path
    assert c.get("/v1/ops/runs/nope").status_code == 404


def test_unfinished_runs_do_not_count_toward_latency() -> None:
    c = make({"a": run()[:3], "b": run(2000)})
    body = c.get("/v1/ops/runs").json()
    assert body["latency"]["runs"] == 1 and len(body["runs"]) == 2


def test_lag_is_summed_per_group_and_a_broker_outage_is_a_503() -> None:
    rows = [
        LagRow("agent-triage.questions", "questions", 0, 3, 5, 2),
        LagRow("agent-triage.questions", "questions", 1, 0, 4, 4),
        LagRow("agent-triage.questions.retry.1", "questions.retry.1", 0, None, 3, 3),
        LagRow("agent-release.drafts", "drafts", 0, 9, 9, 0),
        LagRow("agent-release.reviews", "reviews", 0, 1, 1, 0, members=0),
    ]
    got = make(lag=lambda: rows).get("/v1/ops/lag").json()
    assert got == [
        {"group": "agent-release", "lag": 0, "topics": ["drafts", "reviews"], "consumers_down": 1},
        {"group": "agent-triage", "lag": 9, "topics": ["questions"], "consumers_down": 0},
    ]

    def down() -> list[LagRow]:
        raise RuntimeError("broker down")

    assert make(lag=down).get("/v1/ops/lag").status_code == 503


def test_routes_that_are_not_wired_say_so() -> None:
    c = make()
    assert c.get("/v1/ops/lag").status_code == 404
    assert c.get("/v1/ops/evals").status_code == 404
    assert make(evals=lambda: []).get("/v1/ops/evals").json() == []


def test_the_page_is_served_with_a_nonce_and_never_cached() -> None:
    r = make().get("/ops")
    assert r.status_code == 200 and "Engineering console" in r.text
    assert "<script>" not in r.text and 'nonce="' in r.text
    assert r.headers.get("cache-control") != "public"


def test_in_memory_run_log_lists_newest_first() -> None:
    log = InMemoryRunLog()
    for cid in ("a", "b", "c"):
        log.append(cid, "u", {"event_id": cid, "type": "question.asked", "at": "x"})
    assert [cid for cid, _ in log.recent(2)] == ["c", "b"]


def test_stage_timings_say_where_the_time_goes() -> None:
    from vitalmesh_api.opsview import stage_timings

    fast, slow = summarise_run("a", run(4000)), summarise_run("b", run(9000))
    by = {s.type: s for s in stage_timings([fast, slow])}
    assert by["run.planned"].p50_ms == 150 and by["run.planned"].runs == 2  # same in both
    # the helpers' wait is the gap between planning (150 ms) and advice (2500 ms)
    assert by["advice.completed"].p50_ms == 2350
    # the last step absorbs the difference between the two runs
    assert (by["recommendation.released"].p50_ms, by["recommendation.released"].p95_ms) == (
        1000,
        6000,
    )
    assert stage_timings([]) == []


def test_the_runs_route_carries_the_stage_timings() -> None:
    c = make({"a": run(4000), "b": run(9000), "cache": run(300, "T1")})  # a hit is kept apart
    body = c.get("/v1/ops/runs").json()["stages_by_tier"]
    stages = {s["type"]: s for s in body["T3"]}
    assert stages["advice.completed"]["runs"] == 2 and stages["advice.completed"]["p50_ms"] == 2350
    assert {x["type"]: x["runs"] for x in body["T1"]}["run.planned"] == 1  # its own table
    assert {x["type"]: x["runs"] for x in body["T3"]}["run.planned"] == 2  # not mixed in
    assert [s["type"] for s in body["T3"]][:3] == [
        "question.asked",
        "run.planned",
        "advice.completed",
    ]


def test_the_graph_route_serves_the_compiled_document_or_says_it_is_not_loaded() -> None:
    from vitalmesh_api.opsview import graph_of

    doc = json.dumps(
        {
            "version": "kg-1",
            "edges": [
                {
                    "kind": "avoids_allergen",
                    "condition": "celiac_disease",
                    "target": "gluten",
                    "source": "s",
                },
                {"kind": "limits_intensity", "condition": "cardiac", "target": "3", "source": "s"},
            ],
        }
    )
    got = make(graph=lambda: graph_of(doc)).get("/v1/ops/graph").json()
    assert got["version"] == "kg-1" and [e["condition"] for e in got["edges"]] == [
        "celiac_disease",
        "cardiac",
    ]
    assert make(graph=lambda: graph_of(None)).get("/v1/ops/graph").status_code == 404
    for damaged in (
        "{not json",
        json.dumps({"version": "v"}),
        json.dumps({"version": "v", "edges": [{"kind": "x"}]}),
    ):
        assert graph_of(damaged) is None  # never half shown
    assert make().get("/v1/ops/graph").status_code == 404
