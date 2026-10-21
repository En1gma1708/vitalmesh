"""One way for the T3 agents to call the router: bounded retries on provider trouble, and one
bigger second try when a reasoning model spent its whole budget thinking (D61, D66).

Nothing here hides a failure: after the last attempt it raises ``AgentUnavailable``, which each
agent turns into its own safe outcome (a deterministic fallback, a FAILED advice, an escalation).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from vitalmesh_llm import AllProvidersFailed, LLMRequest, LLMResponse, Message
from vitalmesh_llm.types import Alias, DataClass


class RouterLike(Protocol):
    def complete(self, req: LLMRequest, max_wait_s: float = 0.0) -> LLMResponse: ...


class AgentUnavailable(RuntimeError):
    """No provider could serve the request: not a verdict about the content."""


def complete(
    router: RouterLike,
    *,
    system: str,
    user: str,
    alias: Alias,
    data_class: DataClass,
    purpose: str,
    max_tokens: int = 700,
    json_output: bool = False,
    attempts: int = 2,
    pause_s: float = 5.0,
    max_wait_s: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
) -> LLMResponse:
    def build(tokens: int) -> LLMRequest:
        return LLMRequest(
            messages=(Message(role="system", content=system), Message(role="user", content=user)),
            alias=alias,
            max_tokens=tokens,
            json_output=json_output,
            data_class=data_class,
            purpose=purpose,
        )

    def once(tokens: int) -> LLMResponse:
        last: AllProvidersFailed | None = None
        for attempt in range(attempts):
            try:
                return router.complete(build(tokens), max_wait_s=max_wait_s)
            except AllProvidersFailed as exc:
                last = exc
                if attempt + 1 < attempts:
                    sleep(pause_s)
        raise AgentUnavailable(str(last)[:200]) from last

    resp = once(max_tokens)
    if resp.truncated and not resp.text.strip():
        resp = once(min(2 * max_tokens, 2048))  # the model spent its budget reasoning
    return resp
