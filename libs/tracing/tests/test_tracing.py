from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from vitalmesh_tracing import NULL, LangfuseTracer, mask, pseudonym, tracer_from_env
from vitalmesh_tracing.tracer import MAX_STRING


def test_mask_redacts_emails_and_long_digit_runs_and_truncates() -> None:
    out = mask(
        {"a": "mail me at sahil@example.com or call 9876543210", "b": ["x" * (MAX_STRING + 50)]}
    )
    assert "sahil@example.com" not in out["a"] and "9876543210" not in out["a"]
    assert "[email]" in out["a"] and "[number]" in out["a"]
    assert out["b"][0].endswith("...[truncated]") and len(out["b"][0]) < MAX_STRING + 30
    assert mask(3.5) == 3.5 and mask(None) is None
    assert mask("zone 3, 2124 kcal") == "zone 3, 2124 kcal"  # ordinary numbers survive


def test_pseudonym_is_stable_short_and_not_the_id() -> None:
    assert pseudonym("demo-1") == pseudonym("demo-1") != pseudonym("demo-2")
    assert "demo-1" not in pseudonym("demo-1") and len(pseudonym("demo-1")) == 14


def test_null_tracer_does_nothing_and_never_raises() -> None:
    with NULL.run("r", input="q") as root, NULL.observe("o", as_type="tool") as obs:
        root.update(output="x")
        obs.update(output=1, usage={"input": 1}, model="m")
    NULL.score("s", 1.0)
    NULL.flush()
    assert not NULL.enabled


class FakeObs:
    def __init__(self, kwargs: dict[str, Any], fail: bool = False) -> None:
        self.kwargs, self.fail = kwargs, fail
        self.updates: list[dict[str, Any]] = []

    def update(self, **fields: Any) -> None:
        if self.fail:
            raise RuntimeError("network down")
        self.updates.append(fields)


class FakeClient:
    def __init__(self, fail_update: bool = False) -> None:
        self.observations: list[FakeObs] = []
        self.scores: list[dict[str, Any]] = []
        self.flushed = 0
        self.fail_update = fail_update

    @contextlib.contextmanager
    def start_as_current_observation(self, **kwargs: Any) -> Iterator[FakeObs]:
        obs = FakeObs(kwargs, self.fail_update)
        self.observations.append(obs)
        yield obs

    def score_current_trace(self, **kwargs: Any) -> None:
        self.scores.append(kwargs)

    def flush(self) -> None:
        self.flushed += 1


def make(fail_update: bool = False) -> tuple[LangfuseTracer, FakeClient, list[dict[str, Any]]]:
    client, propagated = FakeClient(fail_update), []

    @contextlib.contextmanager
    def propagate(**attrs: Any) -> Iterator[None]:
        propagated.append(attrs)
        yield

    return LangfuseTracer(client, propagate), client, propagated


def test_a_run_is_an_agent_observation_with_hashed_user_and_tags() -> None:
    tracer, client, propagated = make()
    with tracer.run("answer-x", input="q", user_id="demo-1", session_id="s1", tags=["evidence"]):
        pass
    assert client.observations[0].kwargs == {"name": "answer-x", "as_type": "agent", "input": "q"}
    attrs = propagated[0]
    assert attrs["user_id"] == pseudonym("demo-1") and "demo-1" not in str(attrs)
    assert (
        attrs["session_id"] == "s1"
        and attrs["tags"] == ["evidence"]
        and attrs["trace_name"] == "answer-x"
    )


def test_observe_passes_the_type_model_and_maps_usage_to_usage_details() -> None:
    tracer, client, _ = make()
    with tracer.observe(
        "llm-complete", as_type="generation", input=[{"role": "user"}], model="m1"
    ) as g:
        g.update(
            output="hi", usage={"input": 3, "output": 4}, metadata={"cached": False}, level="ERROR"
        )
    obs = client.observations[0]
    assert obs.kwargs["as_type"] == "generation" and obs.kwargs["model"] == "m1"
    assert obs.updates[0] == {
        "output": "hi",
        "metadata": {"cached": False},
        "usage_details": {"input": 3, "output": 4},
        "level": "ERROR",
    }


def test_scores_use_the_right_data_types() -> None:
    tracer, client, _ = make()
    tracer.score("ok", True)
    tracer.score("status", "supported")
    tracer.score("latency", 1.5)
    assert [(s["name"], s["data_type"]) for s in client.scores] == [
        ("ok", "BOOLEAN"),
        ("status", "CATEGORICAL"),
        ("latency", "NUMERIC"),
    ]
    assert client.scores[0]["value"] == 1.0


def test_a_failing_langfuse_update_never_breaks_the_application() -> None:
    tracer, _, _ = make(fail_update=True)
    with tracer.observe("o") as o:
        o.update(output="x")  # swallowed
    tracer.flush()


def test_application_errors_inside_a_trace_still_propagate() -> None:
    tracer, _, _ = make()
    with pytest.raises(ValueError, match="boom"), tracer.run("r", input="q"):
        raise ValueError("boom")


def test_without_keys_the_env_tracer_is_the_null_tracer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert tracer_from_env(tmp_path / "missing.env") is NULL
    env = tmp_path / ".env"
    env.write_text("LANGFUSE_PUBLIC_KEY=pk-lf-only-one\n", encoding="utf-8")
    assert tracer_from_env(env) is NULL  # a half-configured setup stays off


def test_with_keys_the_client_gets_the_masking_hook_environment_and_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import langfuse

    seen: dict[str, Any] = {}

    class Spy:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(langfuse, "Langfuse", Spy)
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "LANGFUSE_PUBLIC_KEY=\"pk-lf-x\"\nLANGFUSE_SECRET_KEY='sk-lf-y'\n"
        "LANGFUSE_BASE_URL=https://us.cloud.langfuse.com\n",
        encoding="utf-8",
    )
    tracer = tracer_from_env(env, environment="evaluation")
    assert tracer.enabled
    assert (seen["public_key"], seen["secret_key"]) == ("pk-lf-x", "sk-lf-y")  # quotes stripped
    assert (
        seen["base_url"] == "https://us.cloud.langfuse.com" and seen["environment"] == "evaluation"
    )
    assert seen["mask"](data="mail a@b.co") == "mail [email]"
