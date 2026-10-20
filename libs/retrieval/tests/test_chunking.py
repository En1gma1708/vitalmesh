from __future__ import annotations

from vitalmesh_retrieval.chunking import MIN_CHARS, chunk_page, chunk_pages, clean, looks_like_index
from vitalmesh_retrieval.embed import HashEmbedder

SENT = "Adults should do at least 150 minutes of moderate-intensity activity each week. "


def test_clean_joins_wrapped_lines_and_hyphenated_words() -> None:
    assert clean("sleep is impor-\ntant for\nrecovery.") == "sleep is important for recovery."
    assert clean("a\x00b  c") == "a b c"


def test_chunks_respect_the_size_limit_and_overlap_by_one_sentence() -> None:
    sentences = [
        f"Sentence number {i} says something useful about healthy habits today." for i in range(30)
    ]
    chunks = chunk_page(" ".join(sentences), "p. 3", max_chars=400)
    assert len(chunks) > 3
    assert all(len(t) <= 400 + 80 for _, t in chunks)
    first_last = chunks[0][1].split(". ")[-1].rstrip(".")
    assert first_last in chunks[1][1]  # the overlap sentence is repeated
    assert {loc for loc, _ in chunks} == {"p. 3"}


def test_every_sentence_is_kept_and_nothing_crosses_a_page() -> None:
    pages = [SENT * 4, "Sleep seven hours every night to support recovery and health. " * 3]
    chunks = chunk_pages(pages, first_page=10)
    assert [loc for loc, _ in chunks] == ["p. 10", "p. 11"]
    assert "150 minutes" in chunks[0][1] and "Sleep seven" not in chunks[0][1]


def test_short_fragments_and_index_pages_are_dropped() -> None:
    assert chunk_page("Chapter 4", "p. 1") == []
    assert (
        chunk_page(
            "Introduction ........ 3  Methods ........ 9  Results ........ 14  X ..... 2", "p. 2"
        )
        == []
    )
    assert looks_like_index("1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20" * 3)
    assert all(len(t) >= MIN_CHARS for _, t in chunk_page(SENT * 2, "p. 1"))


def test_hash_embedder_is_deterministic_unit_length_and_word_sensitive() -> None:
    e = HashEmbedder(64)
    a = e.embed_query("minutes of moderate activity")
    assert a == e.embed_query("minutes of moderate activity")
    assert abs(sum(x * x for x in a) - 1.0) < 1e-9
    near_v = e.embed_passages(["moderate activity minutes each week"])[0]
    far_v = e.embed_passages(["fruit vegetables whole grains"])[0]
    close = sum(x * y for x, y in zip(a, near_v, strict=True))
    far = sum(x * y for x, y in zip(a, far_v, strict=True))
    assert close > far
