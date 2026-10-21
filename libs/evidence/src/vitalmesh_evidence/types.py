"""Evidence agent data contracts (D64). Every output sentence carries a citation to a corpus
passage, with the exact words that support it, so a reader can check the claim at its source."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Passage(BaseModel):
    """A retrieved chunk the model may cite, addressed by a short ref ("c1") that the model writes
    instead of a long id it could get wrong."""

    model_config = ConfigDict(frozen=True)

    ref: str
    doc_id: str
    locator: str | None
    title: str
    source_url: str
    licence: str
    text: str


class DraftSentence(BaseModel):
    """What the model proposes: one claim, the passage it relies on, and the words it copied."""

    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1)
    cite: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class Draft(BaseModel):
    model_config = ConfigDict(frozen=True)

    insufficient: bool = False
    sentences: tuple[DraftSentence, ...] = ()


class Citation(BaseModel):
    model_config = ConfigDict(frozen=True)

    doc_id: str
    locator: str | None
    title: str
    source_url: str
    licence: str
    quote: str


class AnswerSentence(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    citation: Citation


Status = Literal["supported", "insufficient_evidence", "rejected"]


class EvidenceAnswer(BaseModel):
    """``supported``: every sentence passed verification. ``insufficient_evidence``: retrieval was
    weak or the model said the passages do not answer. ``rejected``: the model's drafts failed
    verification twice, so nothing it wrote is shown."""

    model_config = ConfigDict(frozen=True)

    status: Status
    sentences: tuple[AnswerSentence, ...] = ()
    reason: str = ""
    attempts: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class EvidenceUnavailable(RuntimeError):
    """No LLM provider could serve the request: not an evidence verdict."""
