"""Request and response types for the LLM router.

Policy enforced at this boundary: every request declares ``data_class`` and only ``synthetic``
or ``public`` data may leave the process (project rule: no real personal data to external LLM
APIs). Output length is always bounded (``max_tokens``), and the model is chosen by alias so
callers never hard-code a provider.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["system", "user", "assistant", "tool"]
Alias = Literal["strong", "fast", "mock"]
DataClass = Literal["synthetic", "public"]


class ToolCall(BaseModel):
    """A function call the model asked for. ``arguments`` stays the raw JSON string the provider
    sent, so the caller validates it against its own schema (the model is untrusted)."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: str


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
    description: str = Field(min_length=1)
    parameters: dict[str, Any]  # a JSON Schema object


class Message(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()  # on an assistant message that asked for tools
    tool_call_id: str | None = None  # on a tool message: which call it answers

    @model_validator(mode="after")
    def _has_content_or_calls(self) -> Message:
        if not self.content and not self.tool_calls:
            raise ValueError("a message needs content or tool calls")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("a tool message must say which call it answers")
        return self


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    messages: tuple[Message, ...] = Field(min_length=1)
    alias: Alias = "fast"
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=512, ge=1, le=2048)
    json_output: bool = False
    tools: tuple[ToolSpec, ...] = Field(
        default=(), max_length=8
    )  # function tools the model may call
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
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning: str | None = None  # the model's own reasoning text when the provider returns it

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
