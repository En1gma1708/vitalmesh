"""Local text embeddings (D63): no API quota, no text leaves the machine.

``LocalEmbedder`` runs ``BAAI/bge-small-en-v1.5`` (MIT licence, 384 dimensions) through fastembed's
ONNX runtime on CPU. The model files are downloaded once into a project-local, gitignored cache
(``.cache/fastembed``), not the temp directory, so they survive reboots. bge models expect a
fixed instruction prefix on QUERIES (not on passages); ``embed_query`` adds it.

The embedder is a small Protocol so tests and the corpus builder can use a deterministic stand-in
without downloading anything.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

MODEL_NAME = "BAAI/bge-small-en-v1.5"
MODEL_DIM = 384
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
DEFAULT_CACHE = Path(".cache") / "fastembed"


class Embedder(Protocol):
    dim: int

    def embed_passages(self, texts: Sequence[str]) -> list[tuple[float, ...]]: ...

    def embed_query(self, text: str) -> tuple[float, ...]: ...


class LocalEmbedder:
    dim = MODEL_DIM

    def __init__(self, cache_dir: Path | None = None) -> None:
        from fastembed import TextEmbedding  # imported here: heavy, and optional for tests

        self._model = TextEmbedding(
            model_name=MODEL_NAME, cache_dir=str(cache_dir or DEFAULT_CACHE)
        )

    def embed_passages(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [tuple(float(x) for x in v) for v in self._model.embed(list(texts))]

    def embed_query(self, text: str) -> tuple[float, ...]:
        v = next(iter(self._model.embed([QUERY_PREFIX + text])))
        return tuple(float(x) for x in v)


class HashEmbedder:
    """Deterministic bag-of-words stand-in for tests: each word hashes to a few dimensions, so
    texts sharing words are close in cosine distance. Not a language model; offline and instant."""

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim

    def _vec(self, text: str) -> tuple[float, ...]:
        v = [0.0] * self.dim
        for word in text.lower().split():
            h = hashlib.sha256(word.strip(".,;:!?()").encode()).digest()
            v[int.from_bytes(h[:4], "big") % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return tuple(x / norm for x in v)

    def embed_passages(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._vec(text)
