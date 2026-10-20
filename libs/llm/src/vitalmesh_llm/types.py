"""Request and response types for the LLM router.

Policy enforced at this boundary: every request declares ``data_class`` and only ``synthetic``
or ``public`` data may leave the process (project rule: no real personal data to external LLM
APIs). Output length is always bounded (``max_tokens``), and the model is chosen by alias so
callers never hard-code a provider.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant"]
Alias = Literal["strong", "fast", "mock"]
DataClass = Literal["synthetic", "public"]


class Message(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Role
    content: str = Field(min_length=1)


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    messages: tuple[Message, ...] = Field(min_length=1)
    alias: Alias = "fast"
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=512, ge=1, le=2048)
    json_output: bool = False
    data_class: DataClass  # required: no default, so a caller must state what it is sending
    purpose: str = Field(min_length=1, max_length=64)  # for accounting only, never the prompt


class LLMResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    cached: bool = False
    finish_reason: str | None = None

    @property
    def truncated(self) -> bool:
        """True when the model stopped at ``max_tokens``: reasoning models can spend the whole
        budget thinking and return a cut-off answer ("Your")."""
        return (self.finish_reason or "").lower() in {"length", "max_tokens"}


class LLMError(RuntimeError):
    """Base class: nothing usable came back."""


class AllProvidersFailed(LLMError):
    def __init__(
        self, attempts: list[str], budget_only: bool = False, retry_after_s: float = 0.0
    ) -> None:
        super().__init__("no provider could serve the request: " + "; ".join(attempts))
        self.attempts = attempts
        self.budget_only = budget_only  # every skip was for rate budget alone: waiting can help
        self.retry_after_s = retry_after_s
