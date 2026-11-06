"""The Ops MCP tools (D88): read-only, one GET each, bounded, data-with-a-notice."""

from __future__ import annotations

from typing import Any

import pytest
from mcp import Client
from vitalmesh_ops_mcp.client import OpsError
from vitalmesh_ops_mcp.server import MAX_CHARS, MAX_EDGES, NOTICE, build_server

ANSWERS: dict[str, Any] = {
    "/v1/ops/tiers": {"tiers": {"T3": 2}, "planned": 2},
    "/v1/ops/runs": {"runs": [], "latency": {"runs": 0}},
    "/v1/ops/runs/run-1": {"summary": {"correlation_id": "run-1"}, "entries": []},
    "/v1/ops/lag": [{"group": "agent-triage", "lag": 0}],
    "/v1/ops/evals": [{"name": "deterministic", "passed": 26, "total": 26}],
    "/v1/ops/graph": {"version": "kg-seed-1", "edges": []},
    "/v1/ops/nudges": {"counters": {}, "model_agreement": 0.75},
}
TOOL_PATHS = {
    "ops_tiers": "/v1/ops/tiers",
    "ops_recent_runs": "/v1/ops/runs",
    "ops_consumer_lag": "/v1/ops/lag",
    "ops_eval_baselines": "/v1/ops/evals",
    "ops_knowledge_graph": "/v1/ops/graph",
    "ops_nudges": "/v1/ops/nudges",
}


class FakeApi:
    def __init__(
        self, answers: dict[str, Any] | None = None, error: OpsError | None = None
    ) -> None:
        self.answers = dict(ANSWERS if answers is None else answers)
        self.error = error
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((path, params))
        if self.error is not None:
            raise self.error
        return self.answers[path]


async def call(api: FakeApi, tool: str, args: dict[str, Any] | None = None) -> Any:
    async with Client(build_server(api)) as client:
        return await client.call_tool(tool, args or {})


@pytest.mark.anyio
async def test_every_tool_is_listed_and_read_only() -> None:
    async with Client(build_server(FakeApi())) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {*TOOL_PATHS, "ops_run"}
    for tool in tools.values():
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True and tool.annotations.open_world_hint is False


@pytest.mark.anyio
@pytest.mark.parametrize("tool", sorted(TOOL_PATHS))
async def test_each_tool_is_one_get_to_its_own_route_and_carries_the_notice(tool: str) -> None:
    api = FakeApi()
    result = await call(api, tool)
    assert [path for path, _ in api.calls] == [TOOL_PATHS[tool]]
    out = result.structured_content
    assert out["notice"] == NOTICE and out["truncated"] is False
    expected = ANSWERS[TOOL_PATHS[tool]]
    if tool == "ops_knowledge_graph":  # the graph tool also reports the real edge count
        expected = {**expected, "edge_count": 0}
    assert out["data"] == expected


@pytest.mark.anyio
async def test_the_run_limit_is_passed_on_and_capped() -> None:
    api = FakeApi()
    await call(api, "ops_recent_runs", {"limit": 7})
    assert api.calls == [("/v1/ops/runs", {"limit": 7})]
    for bad in (0, 51, 10_000):
        refused = await call(api, "ops_recent_runs", {"limit": bad})
        assert refused.is_error is True
    assert len(api.calls) == 1  # nothing was fetched for the refused limits


@pytest.mark.anyio
async def test_one_run_is_read_by_a_checked_id() -> None:
    api = FakeApi()
    ok = await call(api, "ops_run", {"correlation_id": "run-1"})
    assert ok.structured_content["data"]["summary"]["correlation_id"] == "run-1"
    for bad in ("../admin", "run/1", "run 1", "a" * 81, "", "run?x=1", "%2e%2e"):
        assert (await call(api, "ops_run", {"correlation_id": bad})).is_error is True
    assert [path for path, _ in api.calls] == [
        "/v1/ops/runs/run-1"
    ]  # bad ids never reached the API


@pytest.mark.anyio
async def test_an_api_problem_is_a_plain_tool_error_without_internals() -> None:
    api = FakeApi(error=OpsError("That account is not an operator, so it cannot read ops data."))
    result = await call(api, "ops_tiers")
    assert result.is_error is True
    assert "not an operator" in result.content[0].text


@pytest.mark.anyio
async def test_a_huge_answer_is_cut_and_says_so() -> None:
    api = FakeApi({"/v1/ops/lag": [{"group": "g" * 1000, "lag": n} for n in range(400)]})
    out = (await call(api, "ops_consumer_lag")).structured_content
    assert out["truncated"] is True and len(out["data"]["preview"]) == MAX_CHARS


@pytest.mark.anyio
async def test_the_graph_is_capped_at_a_hundred_edges_and_reports_the_real_count() -> None:
    edges = [
        {"kind": "avoids_allergen", "condition": f"c{n}", "target": "nuts", "source": "s"}
        for n in range(150)
    ]
    api = FakeApi({"/v1/ops/graph": {"version": "v", "edges": edges}})
    out = (await call(api, "ops_knowledge_graph")).structured_content
    assert len(out["data"]["edges"]) == MAX_EDGES and out["data"]["edge_count"] == 150
    assert out["truncated"] is True
