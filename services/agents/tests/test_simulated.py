"""The simulated model is for load tests only (D91): it must not switch on by accident."""

from __future__ import annotations

import time

import pytest
from vitalmesh_agents.main import Runtime, run_deadline_from_env
from vitalmesh_agents.simulated import (
    ALLOW_ENV,
    ALLOW_VALUE,
    LATENCY_ENV,
    SimulatedRouter,
    simulated_router_from_env,
)
from vitalmesh_llm import LLMRequest, Message


def req(purpose: str) -> LLMRequest:
    return LLMRequest(
        alias="fast",
        purpose=purpose,
        data_class="synthetic",
        messages=[Message(role="user", content="hello")],
    )


def test_refuses_without_the_benchmark_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    with pytest.raises(SystemExit, match="refusing"):
        simulated_router_from_env()


def test_refuses_with_a_wrong_flag_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    with pytest.raises(SystemExit):
        simulated_router_from_env()


def test_reads_the_latency_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, ALLOW_VALUE)
    monkeypatch.setenv(LATENCY_ENV, "25")
    assert simulated_router_from_env().latency_ms == 25.0


def test_a_specialist_calls_its_tool_then_answers() -> None:
    router = SimulatedRouter()
    first = router.complete(req("nutrition-specialist"))
    assert first.tool_calls and first.tool_calls[0].name == "get_energy_target"
    assert first.provider == "simulated"


def test_checker_approves_and_calls_are_counted_without_growing_a_list() -> None:
    router = SimulatedRouter()
    for _ in range(50):
        assert '"approve"' in router.complete(req("checker")).text
    assert router.calls["checker"] == 50
    assert router._inner.purposes == []


def test_waits_for_the_stated_latency() -> None:
    router = SimulatedRouter(latency_ms=60)
    start = time.perf_counter()
    router.complete(req("briefer"))
    assert time.perf_counter() - start >= 0.055


def test_negative_latency_is_rejected() -> None:
    with pytest.raises(ValueError):
        SimulatedRouter(-1)


def test_run_deadline_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VITALMESH_RUN_DEADLINE_S", raising=False)
    assert run_deadline_from_env() == 90.0
    monkeypatch.setenv("VITALMESH_RUN_DEADLINE_S", "300")
    assert run_deadline_from_env() == 300.0


@pytest.mark.parametrize("bad", ["soon", "0", "-5", "4", "99999", "nan"])
def test_run_deadline_rejects_bad_values_at_start(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("VITALMESH_RUN_DEADLINE_S", bad)
    with pytest.raises(SystemExit):
        run_deadline_from_env()


def test_runtime_uses_the_real_router_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VITALMESH_LLM_MODE", raising=False)
    monkeypatch.setenv("VITALMESH_KAFKA_BOOTSTRAP", "127.0.0.1:1")
    assert Runtime(["triage"]).simulated is False


def test_runtime_goes_simulated_only_with_mode_and_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VITALMESH_LLM_MODE", "simulated")
    monkeypatch.setenv("VITALMESH_KAFKA_BOOTSTRAP", "127.0.0.1:1")
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    rt = Runtime(["synthesizer"])
    assert rt.simulated is True
    with pytest.raises(SystemExit):  # mode alone is not enough
        _ = rt.router
    monkeypatch.setenv(ALLOW_ENV, ALLOW_VALUE)
    assert isinstance(rt.router, SimulatedRouter)
