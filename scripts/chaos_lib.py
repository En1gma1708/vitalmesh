"""Scheduled failures for the load harnesses (D92). Bench stack only.

A spec is a comma-separated list; times are seconds after the first publish:

    kill:ROLE@T        kill one process of ROLE (the first one still alive)
    respawn:ROLE@T     start a new process for ROLE (what an orchestrator would do)
    broker@T           restart the bench Kafka container (`docker restart vitalmesh-bench-kafka`)
    outage:T1-T2       the simulated model fails every call between T1 and T2 (agent graph only)

Every action that ran is recorded with the time it ran, so a result file shows what was done to
the system, not what was planned.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

BENCH_BROKER = "vitalmesh-bench-kafka"  # the only container this module will ever touch


@dataclass(frozen=True)
class Action:
    at: float
    kind: str  # kill | respawn | broker | outage_on | outage_off
    target: str = ""


def parse(spec: str) -> list[Action]:
    actions: list[Action] = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        try:
            if part.startswith("outage:"):
                start, _, end = part.removeprefix("outage:").partition("-")
                a, b = float(start), float(end)
                if not 0 <= a < b:
                    raise ValueError("outage needs 0 <= T1 < T2")
                actions += [Action(a, "outage_on"), Action(b, "outage_off")]
            elif part.startswith("broker@"):
                actions.append(Action(float(part.removeprefix("broker@")), "broker"))
            elif part.startswith(("kill:", "respawn:")):
                kind, _, rest = part.partition(":")
                role, _, at = rest.partition("@")
                if not role or not at:
                    raise ValueError("needs ROLE@T")
                actions.append(Action(float(at), kind, role))
            else:
                raise ValueError("unknown action")
        except ValueError as exc:
            raise SystemExit(f"bad --chaos entry {part!r}: {exc}") from None
    return sorted(actions, key=lambda a: a.at)


@dataclass(eq=False)
class Chaos(threading.Thread):
    actions: list[Action]
    handlers: dict[str, Callable[[str], str]]  # kind -> fn(target) -> note
    t0: float = field(default_factory=time.monotonic)
    done: list[dict[str, object]] = field(default_factory=list)
    stop_event: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        super().__init__(daemon=True)

    def run(self) -> None:
        for act in self.actions:
            if self.stop_event.wait(max(0.0, self.t0 + act.at - time.monotonic())):
                return
            fn = self.handlers.get(act.kind)
            started = time.monotonic() - self.t0
            note = fn(act.target) if fn else "no handler (ignored)"
            self.done.append(
                {
                    "planned_s": act.at,
                    "ran_at_s": round(started, 1),
                    "action": act.kind,
                    "target": act.target,
                    "note": note,
                }
            )


def restart_bench_broker(_: str) -> str:
    out = subprocess.run(
        ["docker", "restart", BENCH_BROKER],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return f"docker restart {BENCH_BROKER}: exit {out.returncode}"
