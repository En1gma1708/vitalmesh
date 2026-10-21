"""Tracing behind a small interface (D65).

Follows the Langfuse best practices (fetched from langfuse.com on 2026-10-21):
one trace per unit of work (a root ``agent`` observation per agent run), nested observations with
explicit types (``generation`` for model calls, ``retriever`` for lookups, ``tool`` for tool calls,
``span`` for checks), verb-first stable names with dynamic values in metadata, model name and token
usage on generations, role-labelled message input/output, a hashed user id, tags set at creation,
scores for outcomes determined after the fact, and a ``flush`` before a script exits.

Safety properties:
* ``NullTracer`` is the default and when no keys are configured: tests, CI and offline demos
  produce no network traffic.
* Tracing never raises into the application: every Langfuse call is wrapped.
* No PII by construction: the LLM router only accepts ``synthetic`` or ``public`` data, so that
  is all a generation can contain; user ids are hashed; a last-line mask redacts e-mail addresses
  and long digit runs and truncates very long strings. The mask is a safety net, not the policy.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

log = logging.getLogger("vitalmesh.tracing")

ObservationType = Literal[
    "span", "generation", "agent", "tool", "chain", "retriever", "evaluator", "guardrail"
]
MAX_STRING = 4000
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_LONG_DIGITS = re.compile(r"\d{9,}")


def pseudonym(user_id: str) -> str:
    """A stable, non-reversible id for per-user views without storing the real identifier."""
    return "u_" + hashlib.sha256(user_id.encode()).hexdigest()[:12]


def mask(data: Any) -> Any:
    """Last-line redaction applied to everything sent to Langfuse."""
    if isinstance(data, str):
        text = _LONG_DIGITS.sub("[number]", _EMAIL.sub("[email]", data))
        return text if len(text) <= MAX_STRING else text[:MAX_STRING] + "...[truncated]"
    if isinstance(data, Mapping):
        return {k: mask(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [mask(v) for v in data]
    return data


class Handle(Protocol):
    def update(
        self,
        *,
        output: Any = None,
        metadata: Mapping[str, Any] | None = None,
        model: str | None = None,
        usage: Mapping[str, int] | None = None,
        level: Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"] | None = None,
        status_message: str | None = None,
    ) -> None: ...


class Tracer(Protocol):
    enabled: bool

    def run(
        self,
        name: str,
        *,
        input: Any,
        user_id: str | None = None,
        session_id: str | None = None,
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
        trace_key: str | None = None,
    ) -> contextlib.AbstractContextManager[Handle]: ...

    def observe(
        self,
        name: str,
        *,
        as_type: ObservationType = "span",
        input: Any = None,
        model: str | None = None,
        model_parameters: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> contextlib.AbstractContextManager[Handle]: ...

    def score(self, name: str, value: float | str | bool, comment: str | None = None) -> None: ...

    def flush(self) -> None: ...


class _NullHandle:
    def update(self, **_: Any) -> None:
        return None


class NullTracer:
    enabled = False

    @contextlib.contextmanager
    def run(self, name: str, **_: Any) -> Iterator[Handle]:
        yield _NullHandle()

    @contextlib.contextmanager
    def observe(self, name: str, **_: Any) -> Iterator[Handle]:
        yield _NullHandle()

    def score(self, name: str, value: float | str | bool, comment: str | None = None) -> None:
        return None

    def flush(self) -> None:
        return None


NULL: Tracer = NullTracer()


class _LangfuseHandle:
    def __init__(self, obs: Any) -> None:
        self._obs = obs

    def update(
        self,
        *,
        output: Any = None,
        metadata: Mapping[str, Any] | None = None,
        model: str | None = None,
        usage: Mapping[str, int] | None = None,
        level: Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"] | None = None,
        status_message: str | None = None,
    ) -> None:
        fields: dict[str, Any] = {}
        if output is not None:
            fields["output"] = output
        if metadata:
            fields["metadata"] = dict(metadata)
        if model:
            fields["model"] = model
        if usage:
            fields["usage_details"] = dict(usage)
        if level:
            fields["level"] = level
        if status_message:
            fields["status_message"] = status_message
        try:
            self._obs.update(**fields)
        except Exception:  # tracing must never break the application
            log.warning("langfuse update failed", exc_info=True)


def _inside_trace() -> bool:
    """Whether an observation is open in this context. Asked of OpenTelemetry directly: the
    Langfuse accessor logs a "No active span" warning every time the answer is no."""
    from opentelemetry import trace

    return bool(trace.get_current_span().get_span_context().is_valid)


class LangfuseTracer:
    enabled = True

    def __init__(
        self, client: Any, propagate: Any, inside: Callable[[], bool] | None = None
    ) -> None:
        self._lf = client
        self._propagate = propagate
        self._inside = inside or _inside_trace

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
        trace_key: str | None = None,
    ) -> Iterator[Handle]:
        if self._inside():
            # already inside a trace: this agent is a child of the caller's agent. Opening a new
            # root here would overwrite the trace's name, tags and user (propagate_attributes).
            with self._lf.start_as_current_observation(
                name=name, as_type="agent", input=input
            ) as obs:
                yield _LangfuseHandle(obs)
            return
        # Agents run in separate processes, so a shared ``trace_key`` (the run's correlation id) is
        # what puts all of one question's agents into ONE trace: the trace id is derived from it.
        context: dict[str, str] | None = None
        if trace_key:
            context = {"trace_id": self._lf.create_trace_id(seed=trace_key)}
            name_for_trace = "t3-run"  # one constant name: every agent of the run writes the same
        else:
            name_for_trace = name
        attrs: dict[str, Any] = {"trace_name": name_for_trace, "tags": list(tags)}
        if user_id:
            attrs["user_id"] = pseudonym(user_id)
        if session_id:
            attrs["session_id"] = session_id
        if metadata:
            attrs["metadata"] = {k: str(v)[:200] for k, v in metadata.items()}
        with (
            self._propagate(**attrs),
            self._lf.start_as_current_observation(
                name=name,
                as_type="agent",
                input=input,
                **({"trace_context": context} if context else {}),
            ) as obs,
        ):
            yield _LangfuseHandle(obs)

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
        kwargs: dict[str, Any] = {"name": name, "as_type": as_type}
        if input is not None:
            kwargs["input"] = input
        if model:
            kwargs["model"] = model
        if model_parameters:
            kwargs["model_parameters"] = dict(model_parameters)
        if metadata:
            kwargs["metadata"] = dict(metadata)
        with self._lf.start_as_current_observation(**kwargs) as obs:
            yield _LangfuseHandle(obs)

    def score(self, name: str, value: float | str | bool, comment: str | None = None) -> None:
        try:
            if isinstance(value, bool):
                self._lf.score_current_trace(
                    name=name, value=float(value), data_type="BOOLEAN", comment=comment
                )
            elif isinstance(value, str):
                self._lf.score_current_trace(
                    name=name, value=value, data_type="CATEGORICAL", comment=comment
                )
            else:
                self._lf.score_current_trace(
                    name=name, value=value, data_type="NUMERIC", comment=comment
                )
        except Exception:
            log.warning("langfuse score failed", exc_info=True)

    def flush(self) -> None:
        try:
            self._lf.flush()
        except Exception:
            log.warning("langfuse flush failed", exc_info=True)


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _git_release() -> str | None:
    """The short commit hash, so traces can be compared across code versions."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def tracer_from_env(env_file: Path | None = None, environment: str = "development") -> Tracer:
    """A Langfuse tracer when LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set (environment or
    ``.env``), otherwise the no-op tracer. ``LANGFUSE_BASE_URL`` picks the region."""
    file_vals = _read_env_file(env_file or Path(".env"))

    def get(name: str) -> str:
        return os.environ.get(name) or file_vals.get(name, "")

    public, secret = get("LANGFUSE_PUBLIC_KEY"), get("LANGFUSE_SECRET_KEY")
    if not (public and secret):
        return NULL
    # imported only now: the SDK must see the credentials we pass, not whatever else is in the env
    from langfuse import Langfuse, propagate_attributes

    base = get("LANGFUSE_BASE_URL") or get("LANGFUSE_HOST") or "https://cloud.langfuse.com"
    client = Langfuse(
        public_key=public,
        secret_key=secret,
        base_url=base,
        environment=get("LANGFUSE_TRACING_ENVIRONMENT") or environment,
        release=get("LANGFUSE_RELEASE") or _git_release(),
        mask=lambda *, data, **_: mask(data),
    )
    return LangfuseTracer(client, propagate_attributes)
