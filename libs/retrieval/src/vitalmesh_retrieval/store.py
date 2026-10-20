"""Guideline corpus store on Postgres + pgvector (D62).

Three searches over the same chunks: full-text (Postgres ``ts_rank_cd``, which is NOT BM25: it
has no document-length normalisation tuned like BM25, so we call it full-text), vector (cosine
distance on an HNSW index) and ``hybrid``, which fuses the two rankings with reciprocal rank
fusion (RRF) so neither score scale has to be calibrated against the other.

Every document must carry a licence and a source URL: the corpus is only ever built from open
or public-domain sources, and a chunk without provenance cannot be cited later (the Evidence
agent requires citations). The embedding dimension is fixed per database at migration time.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import psycopg
from pgvector.psycopg import register_vector
from pydantic import BaseModel, ConfigDict, Field, field_validator

RRF_K = 60  # the constant from the original RRF paper; damps the weight of top ranks


class Document(BaseModel):
    model_config = ConfigDict(frozen=True)

    doc_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    title: str = Field(min_length=1)
    source_url: str = Field(pattern=r"^https?://")
    licence: str = Field(min_length=2)  # e.g. "public domain (US government work)", "CC BY 4.0"
    publisher: str = Field(min_length=1)

    @field_validator("licence")
    @classmethod
    def _licence_is_stated(cls, v: str) -> str:
        if v.strip().lower() in {"unknown", "n/a", "none", "tbd"}:
            raise ValueError("a document needs a stated licence")
        return v


class Chunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1)
    embedding: tuple[float, ...]
    locator: str | None = None  # where in the source ("p. 34"), so a citation can point at it


class Hit(BaseModel):
    model_config = ConfigDict(frozen=True)

    doc_id: str
    ordinal: int
    text: str
    score: float
    locator: str | None = None
    title: str
    source_url: str
    licence: str


class RetrievalError(RuntimeError):
    pass


def _migration(dim: int) -> str:
    return f"""
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE TABLE IF NOT EXISTS documents (
        doc_id text PRIMARY KEY,
        title text NOT NULL,
        source_url text NOT NULL,
        licence text NOT NULL,
        publisher text NOT NULL,
        added_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS chunks (
        doc_id text NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
        ordinal int NOT NULL,
        text text NOT NULL,
        locator text,
        tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
        embedding vector({dim}) NOT NULL,
        PRIMARY KEY (doc_id, ordinal)
    );
    CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (tsv);
    CREATE INDEX IF NOT EXISTS chunks_embedding_idx
        ON chunks USING hnsw (embedding vector_cosine_ops);
    """


class Store:
    def __init__(self, dsn: str, dim: int) -> None:
        if not 2 <= dim <= 2000:  # pgvector's HNSW index supports up to 2000 dimensions
            raise ValueError("dim must be between 2 and 2000")
        self._dim = dim
        self._conn = psycopg.connect(dsn, autocommit=True)
        self._conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(self._conn)

    def close(self) -> None:
        self._conn.close()

    def migrate(self) -> None:
        self._conn.execute(_migration(self._dim).encode())

    def reset(self) -> None:
        """Drop the corpus tables (local development and tests only)."""
        self._conn.execute("DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS documents;")

    def add_document(self, doc: Document, chunks: Sequence[Chunk]) -> int:
        """Insert or replace a document and all its chunks in one transaction (idempotent)."""
        if not chunks:
            raise RetrievalError("a document needs at least one chunk")
        for c in chunks:
            if len(c.embedding) != self._dim:
                raise RetrievalError(
                    f"embedding has {len(c.embedding)} dimensions, the store expects {self._dim}"
                )
        with self._conn.transaction():
            self._conn.execute(
                "INSERT INTO documents (doc_id, title, source_url, licence, publisher) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (doc_id) DO UPDATE SET "
                "title = EXCLUDED.title, source_url = EXCLUDED.source_url, "
                "licence = EXCLUDED.licence, publisher = EXCLUDED.publisher",
                (doc.doc_id, doc.title, doc.source_url, doc.licence, doc.publisher),
            )
            self._conn.execute("DELETE FROM chunks WHERE doc_id = %s", (doc.doc_id,))
            with self._conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO chunks (doc_id, ordinal, text, locator, embedding) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    [
                        (doc.doc_id, i, c.text, c.locator, list(c.embedding))
                        for i, c in enumerate(chunks)
                    ],
                )
        return len(chunks)

    def count(self) -> tuple[int, int]:
        d = self._conn.execute("SELECT count(*) FROM documents").fetchone()
        c = self._conn.execute("SELECT count(*) FROM chunks").fetchone()
        return (int(d[0]) if d else 0, int(c[0]) if c else 0)

    # ------------------------------------------------------------------------- search
    _SELECT = (
        "SELECT c.doc_id, c.ordinal, c.text, {score} AS score, d.title, d.source_url, d.licence, "
        "c.locator FROM chunks c JOIN documents d USING (doc_id) "
    )

    def _hits(self, rows: Sequence[Any]) -> list[Hit]:
        return [
            Hit(
                doc_id=r[0],
                ordinal=r[1],
                text=r[2],
                score=float(r[3]),
                title=r[4],
                source_url=r[5],
                licence=r[6],
                locator=r[7],
            )
            for r in rows
        ]

    def search_text(self, query: str, k: int = 5) -> list[Hit]:
        """Rank chunks that match ANY query word. ``websearch_to_tsquery`` requires every word,
        which finds nothing for a natural-language question (measured: D63), so the words are
        OR-ed and ``ts_rank_cd`` orders chunks by how many of them (and how close) they match."""
        tsquery = or_query(query)
        if tsquery is None:
            return []
        sql = (
            self._SELECT.format(score="ts_rank_cd(c.tsv, q)")
            + "CROSS JOIN to_tsquery('english', %s) q "
            "WHERE c.tsv @@ q ORDER BY score DESC, c.doc_id, c.ordinal LIMIT %s"
        )
        return self._hits(self._conn.execute(sql, (tsquery, k)).fetchall())

    def search_vector(self, embedding: Sequence[float], k: int = 5) -> list[Hit]:
        if len(embedding) != self._dim:
            raise RetrievalError(f"query has {len(embedding)} dimensions, expected {self._dim}")
        sql = (
            self._SELECT.format(score="1 - (c.embedding <=> %s::vector)")
            + "ORDER BY c.embedding <=> %s::vector, c.doc_id, c.ordinal LIMIT %s"
        )
        vec = list(embedding)
        return self._hits(self._conn.execute(sql, (vec, vec, k)).fetchall())

    def hybrid(
        self, query: str, embedding: Sequence[float], k: int = 5, pool: int = 20
    ) -> list[Hit]:
        """Reciprocal rank fusion of the full-text and vector rankings (each cut to ``pool``)."""
        return fuse([self.search_text(query, pool), self.search_vector(embedding, pool)], k)


_WORD = re.compile(r"[a-z0-9]+")


def or_query(text: str) -> str | None:
    """``a | b | c`` from the alphanumeric words of ``text`` (safe to pass to ``to_tsquery``:
    nothing else can reach it). None when there are no words."""
    words = dict.fromkeys(_WORD.findall(text.lower()))  # unique, in order
    return " | ".join(words) if words else None


def fuse(rankings: Sequence[Sequence[Hit]], k: int) -> list[Hit]:
    """Reciprocal rank fusion: score = sum over rankings of 1 / (RRF_K + rank). Pure; ties are
    broken by (doc_id, ordinal) so the order never depends on dict or database ordering."""
    by_key: dict[tuple[str, int], Hit] = {}
    fused: dict[tuple[str, int], float] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            key = (hit.doc_id, hit.ordinal)
            by_key.setdefault(key, hit)
            fused[key] = fused.get(key, 0.0) + 1.0 / (RRF_K + rank)
    order = sorted(fused, key=lambda key: (-fused[key], key))[:k]
    return [by_key[key].model_copy(update={"score": fused[key]}) for key in order]
