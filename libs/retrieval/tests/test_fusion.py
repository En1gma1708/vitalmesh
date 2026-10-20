from __future__ import annotations

import pytest
from pydantic import ValidationError
from vitalmesh_retrieval import Document, Hit, fuse
from vitalmesh_retrieval.store import RRF_K


def hit(doc: str, ordinal: int = 0) -> Hit:
    return Hit(
        doc_id=doc,
        ordinal=ordinal,
        text="t",
        score=0.0,
        title="T",
        source_url="https://x",
        licence="L",
    )


def test_a_chunk_ranked_by_both_searches_beats_one_ranked_first_by_only_one() -> None:
    text = [hit("a"), hit("b"), hit("c")]
    vector = [hit("d"), hit("b"), hit("e")]
    out = fuse([text, vector], 5)
    assert out[0].doc_id == "b"  # rank 2 in both beats rank 1 in one
    assert out[0].score == pytest.approx(2 / (RRF_K + 2))


def test_fusion_is_deterministic_on_ties_and_respects_k() -> None:
    a, b = [hit("z")], [hit("a")]
    assert [h.doc_id for h in fuse([a, b], 2)] == ["a", "z"]  # equal scores: sorted by key
    assert len(fuse([[hit("a"), hit("b"), hit("c")]], 2)) == 2
    assert fuse([], 3) == []


def test_distinct_chunks_of_one_document_are_not_merged() -> None:
    out = fuse([[hit("a", 0), hit("a", 1)]], 5)
    assert [(h.doc_id, h.ordinal) for h in out] == [("a", 0), ("a", 1)]


@pytest.mark.parametrize("licence", ["unknown", "N/A", "none", "TBD", "x"])
def test_a_document_without_a_real_licence_is_rejected(licence: str) -> None:
    with pytest.raises(ValidationError):
        Document(doc_id="d1", title="T", source_url="https://x.org", licence=licence, publisher="P")


def test_document_id_and_url_are_validated() -> None:
    ok = {"title": "T", "licence": "CC BY 4.0", "publisher": "P"}
    Document(doc_id="who-activity.2020", source_url="https://who.int/x", **ok)
    with pytest.raises(ValidationError):
        Document(doc_id="Bad Id", source_url="https://who.int/x", **ok)
    with pytest.raises(ValidationError):
        Document(doc_id="d1", source_url="file:///etc/passwd", **ok)


def test_or_query_keeps_only_safe_unique_words() -> None:
    from vitalmesh_retrieval.store import or_query

    assert or_query("How much salt, per DAY? salt") == "how | much | salt | per | day"
    assert or_query("a'; DROP TABLE chunks; --") == "a | drop | table | chunks"
    assert or_query("?!  ") is None
