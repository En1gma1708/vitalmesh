from __future__ import annotations

from dataclasses import dataclass

import pytest
from mcp import Client
from vitalmesh_knowledge_mcp.server import MAX_TEXT, NOTICE, build_server


@dataclass(frozen=True)
class P:
    doc_id: str
    locator: str | None
    title: str
    source_url: str
    licence: str
    text: str


class FakeRetriever:
    def __init__(self, passages: list[P], score: float = 0.8) -> None:
        self.passages, self.score = passages, score
        self.asked: list[str] = []

    def retrieve(self, question: str) -> tuple[list[P], float]:
        self.asked.append(question)
        return list(self.passages), self.score


class FakeCatalogue:
    def sources(self) -> list[tuple[str, str, str, str]]:
        return [("who-2020", "WHO guidelines", "https://example.org/who", "CC BY-NC-SA 3.0 IGO")]


SODIUM = P(
    "who-sodium", "p.4", "Sodium intake", "https://example.org/s", "CC BY", "Under 2,000 mg."
)


def server(passages: list[P] | None = None) -> tuple[object, FakeRetriever]:
    fake = FakeRetriever(passages if passages is not None else [SODIUM])
    return build_server(fake, FakeCatalogue()), fake


@pytest.mark.anyio
async def test_tools_are_listed_read_only() -> None:
    srv, _ = server()
    async with Client(srv) as client:  # type: ignore[arg-type]
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"search_guidelines", "list_sources"}
    for tool in tools.values():
        assert tool.annotations is not None and tool.annotations.read_only_hint is True


@pytest.mark.anyio
async def test_search_returns_licensed_passages_with_a_data_notice() -> None:
    srv, fake = server()
    async with Client(srv) as client:  # type: ignore[arg-type]
        result = await client.call_tool("search_guidelines", {"question": "how much salt?"})
    out = result.structured_content
    assert out is not None and out["notice"] == NOTICE
    assert out["passages"][0]["licence"] == "CC BY" and out["passages"][0]["doc_id"] == "who-sodium"
    assert out["top_score"] == 0.8 and fake.asked == ["how much salt?"]


@pytest.mark.anyio
async def test_long_text_is_truncated_and_says_so() -> None:
    long = P("d", None, "t", "https://example.org", "CC BY", "x" * (MAX_TEXT + 50))
    srv, _ = server([long])
    async with Client(srv) as client:  # type: ignore[arg-type]
        result = await client.call_tool("search_guidelines", {"question": "anything"})
    passage = result.structured_content["passages"][0]
    assert len(passage["text"]) == MAX_TEXT and passage["truncated"] is True


@pytest.mark.anyio
async def test_k_caps_the_number_of_passages() -> None:
    srv, _ = server([SODIUM] * 6)
    async with Client(srv) as client:  # type: ignore[arg-type]
        result = await client.call_tool("search_guidelines", {"question": "salt", "k": 2})
    assert len(result.structured_content["passages"]) == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    "args", [{"question": "ab"}, {"question": "x" * 301}, {"question": "salt", "k": 99}]
)
async def test_bad_arguments_are_rejected_before_retrieval(args: dict[str, object]) -> None:
    srv, fake = server()
    async with Client(srv) as client:  # type: ignore[arg-type]
        result = await client.call_tool("search_guidelines", args)
    assert result.is_error is True and fake.asked == []


@pytest.mark.anyio
async def test_sources_list_the_licence() -> None:
    srv, _ = server()
    async with Client(srv) as client:  # type: ignore[arg-type]
        result = await client.call_tool("list_sources", {})
    assert result.structured_content["sources"][0]["licence"].startswith("CC BY")
