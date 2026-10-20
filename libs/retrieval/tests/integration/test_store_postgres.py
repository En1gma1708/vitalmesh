from __future__ import annotations

import math
from collections.abc import Iterator

import psycopg
import pytest
from vitalmesh_retrieval import Chunk, Document, RetrievalError, Store

pytestmark = pytest.mark.integration

ADMIN_DSN = "postgresql://vitalmesh@127.0.0.1:5432/vitalmesh"
TEST_DSN = "postgresql://vitalmesh@127.0.0.1:5432/vitalmesh_test"
DIM = 8


def unit(i: int) -> tuple[float, ...]:
    """A distinct direction per index, so cosine similarity separates the chunks exactly."""
    v = [0.0] * DIM
    v[i % DIM] = 1.0
    return tuple(v)


@pytest.fixture
def store() -> Iterator[Store]:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = 'vitalmesh_test'")
        if not exists.fetchone():
            admin.execute("CREATE DATABASE vitalmesh_test")  # never touches the real corpus
    s = Store(TEST_DSN, DIM)
    s.reset()
    s.migrate()
    yield s
    s.close()


def doc(doc_id: str, licence: str = "public domain (US government work)") -> Document:
    return Document(
        doc_id=doc_id,
        title=f"Title {doc_id}",
        source_url=f"https://example.org/{doc_id}",
        licence=licence,
        publisher="Example",
    )


def load(s: Store) -> None:
    s.add_document(
        doc("sleep"),
        [
            Chunk(text="Adults should sleep seven or more hours per night.", embedding=unit(0)),
            Chunk(text="Regular bedtimes support healthy sleep habits.", embedding=unit(1)),
        ],
    )
    s.add_document(
        doc("activity", "CC BY 4.0"),
        [
            Chunk(
                text="Adults need 150 minutes of moderate aerobic activity weekly.",
                embedding=unit(2),
            ),
            Chunk(text="Strength training on two days supports muscle health.", embedding=unit(3)),
        ],
    )


def test_migration_is_idempotent_and_counts(store: Store) -> None:
    store.migrate()
    load(store)
    assert store.count() == (2, 4)


def test_text_search_finds_the_matching_chunk_with_its_provenance(store: Store) -> None:
    load(store)
    hits = store.search_text("minutes of aerobic activity", 3)
    assert hits[0].doc_id == "activity" and hits[0].ordinal == 0
    assert hits[0].licence == "CC BY 4.0" and hits[0].source_url == "https://example.org/activity"
    assert store.search_text("zzzunmatchedzzz") == []


def test_vector_search_ranks_by_cosine_similarity(store: Store) -> None:
    load(store)
    near = tuple(0.9 if i == 3 else 0.1 * (i == 2) for i in range(DIM))
    hits = store.search_vector(near, 2)
    assert [(h.doc_id, h.ordinal) for h in hits] == [("activity", 1), ("activity", 0)]
    assert hits[0].score > hits[1].score
    assert all(math.isfinite(h.score) for h in hits)


def test_hybrid_returns_a_chunk_that_only_one_search_finds(store: Store) -> None:
    load(store)
    # the words match "sleep", the vector points at the strength chunk: both must surface
    hits = store.hybrid("sleep hours", unit(3), 4)
    ids = {(h.doc_id, h.ordinal) for h in hits}
    assert ("sleep", 0) in ids and ("activity", 1) in ids


def test_reloading_a_document_replaces_its_chunks(store: Store) -> None:
    load(store)
    store.add_document(doc("sleep"), [Chunk(text="Only one chunk now.", embedding=unit(5))])
    assert store.count() == (2, 3)
    assert store.search_text("bedtimes") == []


def test_wrong_dimension_and_empty_documents_are_refused(store: Store) -> None:
    with pytest.raises(RetrievalError):
        store.add_document(doc("bad"), [Chunk(text="x", embedding=(1.0, 2.0))])
    with pytest.raises(RetrievalError):
        store.add_document(doc("empty"), [])
    with pytest.raises(RetrievalError):
        store.search_vector((1.0,), 1)
    assert store.count() == (0, 0)  # a refused load leaves nothing behind


def test_a_failed_insert_rolls_the_whole_document_back(store: Store) -> None:
    load(store)
    bad = Chunk(text="has a NUL byte \x00 which Postgres text rejects", embedding=unit(1))
    with pytest.raises(psycopg.Error):
        store.add_document(doc("sleep"), [Chunk(text="fine", embedding=unit(0)), bad])
    assert store.count() == (2, 4)  # the old version of the document is intact


def test_text_search_matches_any_word_and_a_locator_round_trips(store: Store) -> None:
    store.add_document(
        doc("loc"),
        [Chunk(text="Strength training two days weekly.", embedding=unit(0), locator="p. 12")],
    )
    hits = store.search_text("how often should adults do strength training", 3)
    assert [(h.doc_id, h.locator) for h in hits] == [("loc", "p. 12")]  # not every word is present
    assert store.search_text("???") == []
