"""The capacity model must never present arithmetic or an assumption as a measurement (D90)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "capacity_model.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("capacity_model", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look the module up by name
    spec.loader.exec_module(mod)
    return mod


def test_every_input_has_a_known_tag() -> None:
    params = load().parameters(1_000_000)
    assert {p.tag for p in params.values()} <= {"measured", "arithmetic", "assumption"}


def test_measured_inputs_name_the_run_they_came_from() -> None:
    for p in load().parameters(1_000_000).values():
        if p.tag == "measured":
            assert any(s in p.note for s in ("baseline_", "replay_", "D81", "D82")), p.name


def test_everything_derived_is_arithmetic() -> None:
    out = load().model(1_000_000)["derived"]
    assert {d["tag"] for d in out.values()} == {"arithmetic"}


def test_more_people_never_need_less() -> None:
    mod = load()
    small, big = mod.model(100_000)["derived"], mod.model(1_000_000)["derived"]
    for key in ("events_peak", "processors_for_peak", "redis_gb", "llm_tokens_day"):
        assert big[key]["value"] > small[key]["value"], key


def test_redis_memory_is_users_times_measured_bytes() -> None:
    d = load().model(1_000_000)["derived"]
    assert abs(d["redis_gb"]["value"] - 0.711) < 1e-9
