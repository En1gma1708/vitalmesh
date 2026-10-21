"""A specialist is a LangGraph tool-calling agent with hard limits (D66, generalised in D68).

    agent -> (tool calls?) -> tools -> agent ... -> verify -> (problems and attempt 1? agent again)
                                                           -> finish

Objective: answer a question about THIS user in at most ~100 words using only what its tools
return. Tools: any object with ``specs`` and ``call`` (``NutritionTools``, ``MovementTools``,
``ConditionTools``). Output schema: ``Advice``. Limits: at most ``MAX_MODEL_CALLS`` model calls
(a run can lower it), at most ``MAX_CALLS_PER_ROUND`` tool calls per round, 2 drafts,
``max_tokens`` bounded. One specialist = one ``SpecialistConfig`` (name, system prompt, fallback);
the loop and every check are shared, so a new specialist cannot skip them.

The model is untrusted. It chooses which tool to call and phrases the result; it never supplies a
number. ``check_answer`` rejects any number in the prose that does not occur in a tool result, and
a draft that fails twice is replaced by a deterministic text built from the tool results, so the
user always gets either checked prose or plain facts, never an unchecked claim.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, ConfigDict
from vitalmesh_llm import AllProvidersFailed, LLMRequest, Message, ToolCall, ToolSpec
from vitalmesh_llm.types import Alias, DataClass, LLMResponse
from vitalmesh_tracing import NULL, Tracer

from vitalmesh_specialists.tools import ToolResult

MAX_MODEL_CALLS = 5
MAX_CALLS_PER_ROUND = 3
MAX_DRAFTS = 2
MAX_WORDS = 120

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
# thousands written with a no-break or thin space count as one number; a plain space does not
_SPACED_GROUPS = re.compile("(?<=\\d)[\u00a0\u202f\u2009](?=\\d{3}(?!\\d))")


def numbers(text: str) -> set[float]:
    out: set[float] = set()
    for m in _NUMBER.findall(_SPACED_GROUPS.sub("", text)):
        try:
            out.add(float(m.replace(",", "")))
        except ValueError:
            continue
    return out


def close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.51, 0.005 * abs(b))  # whole-number rounding is allowed


def check_answer(text: str, results: Sequence[ToolResult], max_words: int = MAX_WORDS) -> list[str]:
    """Problems with the model's prose, judged against what the tools actually returned."""
    problems: list[str] = []
    if not text.strip():
        problems.append("the answer is empty")
    if len(re.findall(r"\b\w+\b", text)) > max_words:
        problems.append(f"the answer is longer than {max_words} words")
    allowed: set[float] = set()
    for r in results:
        if r.ok:
            allowed |= numbers(r.to_message_content())
    stray = sorted(v for v in numbers(text) if not any(close(v, a) for a in allowed))
    if stray:
        problems.append(f"numbers {stray} are not in any tool result")
    return problems


class Advice(BaseModel):
    """One specialist's checked contribution. ``results`` are the tool results behind it, so a
    caller can build typed claims and citations from them instead of parsing the prose."""

    model_config = ConfigDict(frozen=True)

    status: Literal["answered", "fallback"]
    text: str
    tools_used: tuple[str, ...] = ()
    model_calls: int = 0
    reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    results: tuple[ToolResult, ...] = ()


class ToolsLike(Protocol):
    @property
    def specs(self) -> tuple[ToolSpec, ...]: ...

    def call(self, name: str, arguments: str) -> ToolResult: ...


@dataclass(frozen=True)
class SpecialistConfig:
    """Everything that differs between specialists. The loop and the checks do not."""

    name: str  # "nutrition": trace names, scores, tags and the request purpose derive from it
    system_prompt: str
    fallback: Callable[[Sequence[ToolResult]], str]
    max_words: int = MAX_WORDS


class SpecialistUnavailable(RuntimeError):
    """No LLM provider could serve the request: not an advice verdict."""


class RouterLike(Protocol):
    def complete(self, req: LLMRequest, max_wait_s: float = 0.0) -> LLMResponse: ...


class _State(TypedDict, total=False):
    messages: list[Message]
    pending: tuple[ToolCall, ...]
    results: list[ToolResult]
    calls: int
    drafts: int
    text: str
    problems: list[str]
    advice: Advice
    prompt_tokens: int
    completion_tokens: int


class ToolSpecialist:
    def __init__(
        self,
        config: SpecialistConfig,
        router: RouterLike,
        alias: Alias = "fast",
        data_class: DataClass = "synthetic",
        max_wait_s: float = 120.0,
        tracer: Tracer = NULL,
        attempts: int = 3,
        retry_pause_s: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        max_model_calls: int = MAX_MODEL_CALLS,
    ) -> None:
        if not 1 <= max_model_calls <= MAX_MODEL_CALLS:
            raise ValueError(f"max_model_calls must be between 1 and {MAX_MODEL_CALLS}")
        self._config = config
        self._max_calls = max_model_calls
        self._attempts = attempts
        self._retry_pause_s = retry_pause_s
        self._sleep = sleep
        self._router = router
        self._alias: Alias = alias
        self._data_class: DataClass = data_class
        self._max_wait_s = max_wait_s
        self._tracer = tracer
        self._tools: ToolsLike | None = None
        self._graph = self._build()

    # ----------------------------------------------------------------------- graph
    def _build(self) -> Any:
        g = StateGraph(_State)
        g.add_node("agent", self._agent)
        g.add_node("tools", self._run_tools)
        g.add_node("verify", self._verify)
        g.add_node("finish", self._finish)
        g.set_entry_point("agent")
        g.add_conditional_edges(
            "agent", lambda s: "tools" if s.get("pending") else "verify", ["tools", "verify"]
        )
        g.add_edge("tools", "agent")
        g.add_conditional_edges(
            "verify",
            lambda s: (
                "agent"
                if s["problems"] and s["drafts"] < MAX_DRAFTS and s["calls"] < self._max_calls
                else "finish"
            ),
            ["agent", "finish"],
        )
        g.add_edge("finish", END)
        return g.compile()

    def _agent(self, state: _State) -> _State:
        assert self._tools is not None
        last_chance = state["calls"] >= self._max_calls - 1
        specs = () if last_chance else self._tools.specs  # the last call must be an answer
        resp = self._call(state["messages"], specs, 900)
        pt = state["prompt_tokens"] + resp.prompt_tokens
        ct = state["completion_tokens"] + resp.completion_tokens
        if resp.truncated and not resp.text.strip() and not resp.tool_calls:
            # the model spent its whole budget reasoning: ask again with more room
            resp = self._call(state["messages"], specs, 1800)
            pt += resp.prompt_tokens
            ct += resp.completion_tokens
        calls = state["calls"] + 1
        if resp.tool_calls and not last_chance:
            asked = resp.tool_calls[:MAX_CALLS_PER_ROUND]
            msg = Message(role="assistant", content=resp.text, tool_calls=asked)
            return {
                "messages": [*state["messages"], msg],
                "pending": asked,
                "calls": calls,
                "prompt_tokens": pt,
                "completion_tokens": ct,
            }
        return {
            "text": resp.text.strip(),
            "pending": (),
            "calls": calls,
            "prompt_tokens": pt,
            "completion_tokens": ct,
        }

    def _call(
        self, messages: Sequence[Message], specs: tuple[ToolSpec, ...], max_tokens: int
    ) -> LLMResponse:
        req = LLMRequest(
            messages=(Message(role="system", content=self._config.system_prompt), *messages),
            alias=self._alias,
            max_tokens=max_tokens,
            tools=specs,
            data_class=self._data_class,
            purpose=f"{self._config.name}-specialist",
        )
        last: AllProvidersFailed | None = None
        for attempt in range(self._attempts):
            try:
                return self._router.complete(req, max_wait_s=self._max_wait_s)
            except AllProvidersFailed as exc:  # transient provider trouble: pause, then retry
                last = exc
                if attempt + 1 < self._attempts:
                    self._sleep(self._retry_pause_s)
        raise SpecialistUnavailable(str(last)[:200]) from last

    def _run_tools(self, state: _State) -> _State:
        assert self._tools is not None
        messages = list(state["messages"])
        results = list(state["results"])
        for call in state["pending"]:
            with self._tracer.observe(call.name, as_type="tool", input=call.arguments) as span:
                result = self._tools.call(call.name, call.arguments)
                span.update(
                    output=result.payload if result.ok else {"error": result.error},
                    level=None if result.ok else "WARNING",
                )
            results.append(result)
            messages.append(
                Message(role="tool", content=result.to_message_content(), tool_call_id=call.id)
            )
        return {"messages": messages, "results": results, "pending": ()}

    def _verify(self, state: _State) -> _State:
        with self._tracer.observe(
            "verify-advice", as_type="guardrail", input=state.get("text", "")[:500]
        ) as span:
            problems = check_answer(state.get("text", ""), state["results"], self._config.max_words)
            span.update(output={"passed": not problems, "problems": problems})
        drafts = state["drafts"] + 1
        if not problems or drafts >= MAX_DRAFTS:
            return {"problems": problems, "drafts": drafts}
        feedback = "Your answer failed these checks; fix them and answer again:\n- " + "\n- ".join(
            problems
        )
        messages = [
            *state["messages"],
            Message(role="assistant", content=state.get("text") or "(empty)"),
            Message(role="user", content=feedback),
        ]
        return {"problems": problems, "drafts": drafts, "messages": messages}

    def _finish(self, state: _State) -> _State:
        used = tuple(dict.fromkeys(r.name for r in state["results"] if r.ok))
        common: dict[str, Any] = {
            "tools_used": used,
            "model_calls": state["calls"],
            "prompt_tokens": state["prompt_tokens"],
            "completion_tokens": state["completion_tokens"],
            "results": tuple(state["results"]),
        }
        if state["problems"] or not state.get("text"):
            reason = "; ".join(state["problems"])[:300] or "no answer was produced"
            advice = Advice(
                status="fallback",
                text=self._config.fallback(state["results"]),
                reason=reason,
                **common,
            )
        else:
            advice = Advice(status="answered", text=state["text"], **common)
        return {"advice": advice}

    # ------------------------------------------------------------------------- run
    def advise(
        self,
        question: str,
        tools: ToolsLike,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> Advice:
        self._tools = tools
        name = self._config.name
        with self._tracer.run(
            f"advise-{name}",
            input=question,
            user_id=user_id,
            session_id=session_id,
            tags=[name],
            metadata={"alias": self._alias},
            trace_key=session_id,
        ) as root:
            result = self._graph.invoke(
                {
                    "messages": [Message(role="user", content=question)],
                    "pending": (),
                    "results": [],
                    "calls": 0,
                    "drafts": 0,
                    "problems": [],
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                },
                {"recursion_limit": 40},
            )
            advice: Advice = result["advice"]
            root.update(
                output={"status": advice.status, "answer": advice.text},
                metadata={"tools": list(advice.tools_used), "model_calls": advice.model_calls},
            )
            self._tracer.score(f"{name}-status", advice.status)
            self._tracer.score("advice-verified", advice.status == "answered")
        return advice
