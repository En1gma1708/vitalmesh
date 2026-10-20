"""Guideline corpus store (Postgres + pgvector)."""

from vitalmesh_retrieval.store import (
    Chunk,
    Document,
    Hit,
    RetrievalError,
    Store,
    fuse,
)

__all__ = ["Chunk", "Document", "Hit", "RetrievalError", "Store", "fuse"]
