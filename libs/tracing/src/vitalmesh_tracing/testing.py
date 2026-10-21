"""A tracer that records what would be sent, for tests (no network, no Langfuse)."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from vitalmesh_tracing.tracer import Handle, ObservationType


@dataclass
class Recorded:
    name: str
    kind: str  # an observation type, or "run" for a root
    parent: str | None
    input: Any = None
    attrs: dict[str, Any] = field(default_factory=dict)  # model, tags, user_id, session_id, ...
    updates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def output(self) -> Any:
        for u in reversed(self.updates):
            if "output" in u:
                return u["output"]
        return None

    @property
    def metadata(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for u in self.updates:
            merged.update(u.get("metadata") or {})
        return merged


class _Handle:
    def __init__(self, rec: Recorded) -> None:
        self._rec = rec

    def update(self, **fields: Any) -> None:
        self._rec.updates.append({k: v for k, v in fields.items() if v is not None})


class RecordingTracer:
    enabled = True

    def __init__(self) -> None:
        self.records: list[Recorded] = []
        self.scores: list[tuple[str, Any]] = []
        self.flushes = 0
        self._stack: list[str] = []

    @contextlib.contextmanager
    def run(
        self,
        name: str,
        *,
        input: Any,
        user_id: str | None = None,
        session_id: str | None = None,
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> Iterator[Handle]:
        rec = Recorded(
            name,
            "run",
            None,
            input,
            {"user_id": user_id, "session_id": session_id, "tags": list(tags)},
        )
        yield from self._open(rec)

    @contextlib.contextmanager
    def observe(
        self,
        name: str,
        *,
        as_type: ObservationType = "span",
        input: Any = None,
        model: str | None = None,
        model_parameters: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Iterator[Handle]:
        rec = Recorded(
            name,
            as_type,
            self._stack[-1] if self._stack else None,
            input,
            {"model": model, "model_parameters": dict(model_parameters or {})},
        )
        if metadata:
            rec.updates.append({"metadata": dict(metadata)})
        yield from self._open(rec)

    def _open(self, rec: Recorded) -> Iterator[Handle]:
        self.records.append(rec)
        self._stack.append(rec.name)
        try:
            yield _Handle(rec)
        finally:
            self._stack.pop()

    def score(self, name: str, value: float | str | bool, comment: str | None = None) -> None:
        self.scores.append((name, value))

    def flush(self) -> None:
        self.flushes += 1

    def named(self, name: str) -> list[Recorded]:
        return [r for r in self.records if r.name == name]
