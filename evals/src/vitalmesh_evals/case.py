"""Eval case and result types.

A case is data: an input, the system under test (SUT) that should handle it, and the graders
its output must satisfy. Graders are deterministic code (D39); nothing here calls a model to
judge another model. Expected values in cases are literals, not recomputed by the code under
test, so a regression in the engine cannot silently move its own goalposts.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SutName = Literal["t0", "explain", "evidence"]


class GraderSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: str
    args: dict[str, Any] = Field(default_factory=dict)


class EvalCase(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    sut: SutName
    input: dict[str, Any]
    graders: tuple[GraderSpec, ...] = Field(min_length=1)
    note: str = ""


class GradeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    grader: str
    passed: bool
    detail: str = ""


class CaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    category: str
    sut: str
    passed: bool
    grades: tuple[GradeResult, ...]
    output: dict[str, Any]
    error: str | None = None
    provider: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached: bool = False
    latency_ms: float = 0.0


def g(type_: str, **args: Any) -> GraderSpec:
    """Short constructor used by the case modules."""
    return GraderSpec(type=type_, args=args)
