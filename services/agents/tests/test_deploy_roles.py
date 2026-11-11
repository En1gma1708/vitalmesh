"""The deployment starts every role the graph needs (D108: the Azure unit once lacked `progress`).

A specialist that is routed to but has no consumer makes its questions wait for the deadline and
come back partial, so the VM's unit file and the runbook must list every specialist role.
"""

from __future__ import annotations

import re
from pathlib import Path

from vitalmesh_agents.main import _SPECIALISTS, INPUTS

ROOT = Path(__file__).resolve().parents[3]
CORE = {"triage", "aggregator", "synthesizer", "guardian", "checker", "release", "tracker"}


def roles_after(text: str, marker: str) -> set[str]:
    line = next(ln for ln in text.splitlines() if marker in ln)
    return set(re.findall(r"[a-z]+", line.split(marker, 1)[1].split("`")[0]))


def test_every_specialist_role_is_a_known_consumer() -> None:
    assert set(_SPECIALISTS) <= set(INPUTS)


def test_the_azure_unit_starts_every_specialist_and_core_role() -> None:
    unit = (ROOT / "infra/azure-app/remote/units/vitalmesh-agents.service").read_text("utf-8")
    started = roles_after(unit, "-m vitalmesh_agents")
    assert set(_SPECIALISTS) | CORE <= started, (set(_SPECIALISTS) | CORE) - started


def test_the_runbook_starts_every_specialist_and_core_role() -> None:
    runbook = (ROOT / "docs/RUNBOOK.md").read_text("utf-8")
    started = roles_after(runbook, "-m vitalmesh_agents")
    assert set(_SPECIALISTS) | CORE <= started, (set(_SPECIALISTS) | CORE) - started
