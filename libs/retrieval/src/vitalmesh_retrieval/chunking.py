"""Split page text into overlapping, sentence-aligned chunks that never cross a page, so every
chunk has an exact page locator for citation. Pure functions; no I/O."""

from __future__ import annotations

import re
from collections.abc import Sequence

_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'])")
_SPACES = re.compile(r"[ \t\u00a0]+")
MIN_CHARS = 120  # shorter fragments are headings, page furniture or list stubs


def clean(text: str) -> str:
    """Normalise whitespace; join hard-wrapped lines but keep paragraph breaks as spaces."""
    text = text.replace("\x00", " ")
    text = re.sub(r"-\n(?=[a-z])", "", text)  # re-join words hyphenated at a line break
    text = re.sub(r"\s*\n\s*", " ", text)
    return _SPACES.sub(" ", text).strip()


def looks_like_index(text: str) -> bool:
    """Tables of contents and indexes: mostly dot leaders or numbers, no prose."""
    if text.count("....") >= 3:
        return True
    letters = sum(c.isalpha() for c in text)
    return len(text) > 0 and letters / len(text) < 0.5


def chunk_page(
    text: str, locator: str, max_chars: int = 900, overlap_sentences: int = 1
) -> list[tuple[str, str]]:
    """Return [(locator, chunk_text)] for one page."""
    text = clean(text)
    if not text or looks_like_index(text):
        return []
    sentences = [s for s in _SENTENCE.split(text) if s]
    out: list[tuple[str, str]] = []
    current: list[str] = []
    size = 0
    for sentence in sentences:
        if current and size + len(sentence) + 1 > max_chars:
            out.append((locator, " ".join(current)))
            current = current[-overlap_sentences:] if overlap_sentences else []
            size = sum(len(x) + 1 for x in current)
        current.append(sentence)
        size += len(sentence) + 1
    if current:
        out.append((locator, " ".join(current)))
    return [(loc, t) for loc, t in out if len(t) >= MIN_CHARS]


def chunk_pages(
    pages: Sequence[str], first_page: int = 1, max_chars: int = 900
) -> list[tuple[str, str]]:
    chunks: list[tuple[str, str]] = []
    for i, page in enumerate(pages):
        chunks.extend(chunk_page(page, f"p. {first_page + i}", max_chars))
    return chunks
