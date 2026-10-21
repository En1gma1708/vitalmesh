"""Entry point: ``uv run python -m vitalmesh_knowledge_mcp`` serves the corpus over MCP.

Streamable HTTP, stateless, loopback only: http://127.0.0.1:8765/mcp (env VITALMESH_MCP_PORT,
VITALMESH_PG_DSN). Needs the Postgres corpus (scripts/build_corpus.py) and the local embedding
model.
"""

from __future__ import annotations

import os

from vitalmesh_evidence.agent import StoreRetriever
from vitalmesh_retrieval.embed import LocalEmbedder
from vitalmesh_retrieval.store import Store

from vitalmesh_knowledge_mcp.server import build_server

DB_DSN = os.environ.get("VITALMESH_PG_DSN", "postgresql://vitalmesh@127.0.0.1:5432/vitalmesh")


class StoreCatalogue:
    def __init__(self, store: Store) -> None:
        self._store = store

    def sources(self) -> list[tuple[str, str, str, str]]:
        return self._store.documents()


def main() -> None:
    embedder = LocalEmbedder()
    store = Store(DB_DSN, embedder.dim)
    server = build_server(StoreRetriever(store, embedder, k=8), StoreCatalogue(store))
    server.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=int(os.environ.get("VITALMESH_MCP_PORT", "8765")),
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
