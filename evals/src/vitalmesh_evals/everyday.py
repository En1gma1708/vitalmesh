"""Everyday health questions, end to end with EVERY MODEL DOWN (D108).

Each question goes through the real in-process agent graph (triage, helpers, aggregator,
synthesizer, Guardian gate, release) with every model call failing, so what is graded is the part
that must always work: the routing, the helper's calculators, the cited reference sentences, the
assembled answer and the Guardian's release. Nothing here calls a model, so it is free, fast and
exact, and it runs in the unit test suite.

Rules (CLAUDE.md, evals): expected values are LITERALS worked out by hand from the definitions
(BMI = kg / m^2 and so on), never recomputed by the code under test; a grader is never loosened
without a written reason in docs/DECISIONS.md; the held-out group is written once, is never used
to tune routing phrases or wording, and its result is only reported.

Run: ``uv run python -m vitalmesh_evals.everyday [--json out.json] [--group tuning|heldout]``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from vitalmesh_agents.inprocess import ask
from vitalmesh_agents.testing import T0, ScriptedRouter, world
from vitalmesh_common.run import specialist_from_proto
from vitalmesh_llm import AllProvidersFailed, LLMRequest, LLMResponse
from vitalmesh_stores import InMemoryHistoryStore, Point
from vitalmesh_wellness.weightgoal import unsafe_weight_request

PROGRESS = frozenset({"progress"})
NUTRITION = frozenset({"nutrition"})
MOVEMENT = frozenset({"movement_recovery"})

# ----------------------------------------------------------------------------------- the people
BODIES: dict[str, dict[str, Any]] = {
    # BMI 31.0, healthy range 56.7 to 76.2 kg, goal lose: a 5 percent milestone of 90.2 kg
    "A": {"sex": "female", "age_years": 35, "weight_kg": 95, "height_cm": 175, "goal": "lose"},
    # BMI 24.7, healthy range 60.0 to 80.6 kg, maintaining
    "B": {"sex": "male", "age_years": 40, "weight_kg": 80, "height_cm": 180, "goal": "maintain"},
    # BMI 17.6: underweight, no loss target
    "C": {"sex": "female", "age_years": 28, "weight_kg": 48, "height_cm": 165, "goal": "lose"},
    # age 70, BMI 27.8: the range but no loss target
    "D": {"sex": "male", "age_years": 70, "weight_kg": 85, "height_cm": 175, "goal": "lose"},
    # BMI 50.8: a weight plan needs a doctor
    "E": {"sex": "female", "age_years": 45, "weight_kg": 130, "height_cm": 160, "goal": "lose"},
    # BMI 22.1, goal gain: no weight target
    "F": {"sex": "male", "age_years": 30, "weight_kg": 70, "height_cm": 178, "goal": "gain"},
}


def _add(
    h: InMemoryHistoryStore, metric: str, values: list[float], unit: str, every: float = 1.0
) -> None:
    n = len(values)
    for i, v in enumerate(values):
        h.add("u-1", metric, Point(t=T0 - timedelta(days=(n - 1 - i) * every), value=v, unit=unit))


def history(name: str) -> InMemoryHistoryStore:
    h = InMemoryHistoryStore()
    if name in ("sleep_ok", "all"):
        _add(h, "sleep_duration", [7.5, 8.0, 7.0, 7.0, 8.0, 7.5, 7.0], "h")  # average 7.4
    if name == "sleep_poor":
        _add(h, "sleep_duration", [6.0, 5.5, 6.5, 6.0, 6.0, 5.0, 6.5], "h")  # average 5.9
    if name in ("weight_down", "all"):
        _add(h, "body_weight", [95 - 0.1 * i for i in range(15)], "kg")  # 0.7 kg a week down
    if name == "weight_stale":
        for i in range(4):
            h.add(
                "u-1", "body_weight", Point(t=T0 - timedelta(days=30 + 3 * i), value=95, unit="kg")
            )
    if name == "all":
        _add(h, "active_minutes", [30, 20, 40, 0, 35, 25, 10], "min")
        _add(h, "energy_intake", [2000, 2100, 1900, 2050, 1950, 2000, 2000], "kcal")
    return h


# ----------------------------------------------------------------------------------- the cases
@dataclass(frozen=True)
class Case:
    id: str
    group: str  # tuning | heldout
    question: str
    route: frozenset[str] | None  # expected specialists; None for a screen case
    body: str = "A"
    hist: str = "none"
    contains: tuple[str, ...] = ()
    avoids: tuple[str, ...] = ("ideal weight", "diagnos", "you have diabetes")
    decline: str | None = None  # screen cases: the expected reason code (None = must pass)
    screen: bool = False
    notes: str = field(default="", compare=False)


def graph(cid: str, q: str, route: frozenset[str], group: str = "tuning", **kw: Any) -> Case:
    return Case(id=cid, group=group, question=q, route=route, **kw)


def screen(cid: str, q: str, decline: str | None, body: str = "A") -> Case:
    return Case(
        id=cid, group="tuning", question=q, route=None, body=body, decline=decline, screen=True
    )


CASES: tuple[Case, ...] = (
    # --- how am I doing (status)
    graph(
        "status-01", "how am i doing", PROGRESS, contains=("BMI is 31.0", "Sleep: not enough data")
    ),
    graph("status-02", "How am I doing this week?", PROGRESS, contains=("BMI is 31.0",)),
    graph("status-03", "how's my progress", PROGRESS, contains=("BMI is 31.0",)),
    graph("status-04", "am I on track with my goals?", PROGRESS, contains=("BMI is 31.0",)),
    graph("status-05", "give me a summary of my health", PROGRESS, contains=("BMI is 31.0",)),
    graph("status-06", "how are things going with my health", PROGRESS, contains=("BMI is 31.0",)),
    graph("status-07", "what's my status", PROGRESS, contains=("BMI is 31.0",)),
    graph("status-08", "how am i progressing", PROGRESS, contains=("BMI is 31.0",)),
    graph(
        "status-09",
        "how am i doing",
        PROGRESS,
        hist="all",
        contains=(
            "averaged 7.4 hours",
            "160 active minutes",
            "Sleep: on track",
            "Activity: on track",
        ),
    ),
    # --- sleep
    graph(
        "sleep-01",
        "how was my sleep",
        PROGRESS,
        hist="sleep_ok",
        contains=("averaged 7.4 hours", "7\u20138 hours", "Sleep status: on track"),
    ),
    graph(
        "sleep-02",
        "How did I sleep this week?",
        PROGRESS,
        hist="sleep_ok",
        contains=("averaged 7.4 hours",),
    ),
    graph(
        "sleep-03",
        "am I sleeping enough?",
        PROGRESS,
        hist="sleep_poor",
        contains=("averaged 5.9 hours", "Sleep status: needs attention", "at least 7"),
    ),
    graph(
        "sleep-04",
        "is my sleep okay",
        PROGRESS,
        hist="sleep_poor",
        contains=("averaged 5.9 hours",),
    ),
    graph(
        "sleep-05",
        "how has my sleep been lately",
        PROGRESS,
        hist="sleep_ok",
        contains=("averaged 7.4 hours",),
    ),
    graph(
        "sleep-06",
        "do I get enough sleep",
        PROGRESS,
        hist="sleep_poor",
        contains=("averaged 5.9 hours",),
    ),
    graph(
        "sleep-07",
        "what's my average sleep",
        PROGRESS,
        hist="sleep_ok",
        contains=("averaged 7.4 hours",),
    ),
    graph(
        "sleep-08",
        "how was my sleep",
        PROGRESS,
        hist="none",
        contains=("need at least 3", "Log your sleep"),
        avoids=("averaged",),
    ),
    # --- weight
    graph(
        "weight-01",
        "how is my weight",
        PROGRESS,
        hist="weight_down",
        contains=("93.6 kg", "0.7 kg a week down", "from 15 readings"),
    ),
    graph(
        "weight-02",
        "how's my weight trend",
        PROGRESS,
        hist="weight_down",
        contains=("0.7 kg a week down",),
    ),
    graph(
        "weight-03",
        "am I losing weight",
        PROGRESS,
        hist="weight_down",
        contains=("0.7 kg a week down",),
    ),
    graph("weight-04", "have I gained weight", PROGRESS, hist="weight_down", contains=("93.6 kg",)),
    graph("weight-05", "what do I weigh", PROGRESS, hist="weight_down", contains=("93.6 kg",)),
    graph(
        "weight-06",
        "what is my current weight",
        PROGRESS,
        hist="weight_down",
        contains=("93.6 kg",),
    ),
    graph(
        "weight-07",
        "has my weight changed this month",
        PROGRESS,
        hist="weight_down",
        contains=("93.6 kg",),
    ),
    graph(
        "weight-08",
        "how is my weight",
        PROGRESS,
        hist="none",
        contains=("not logged a weight yet", "95.0 kg", "Log your weight"),
        avoids=("trend is",),
    ),
    graph(
        "weight-09",
        "how is my weight",
        PROGRESS,
        hist="weight_stale",
        contains=("more than two weeks old",),
    ),
    # --- BMI and target weight
    graph(
        "bmi-01", "what is my bmi", PROGRESS, contains=("BMI is 31.0", "obesity", "56.7 to 76.2 kg")
    ),
    graph("bmi-02", "what's my BMI?", PROGRESS, contains=("BMI is 31.0",)),
    graph("bmi-03", "am I overweight", PROGRESS, contains=("BMI is 31.0",)),
    graph("bmi-04", "am I a healthy weight", PROGRESS, contains=("BMI is 31.0",)),
    graph(
        "target-01",
        "what should my target weight be",
        PROGRESS,
        contains=(
            "56.7 to 76.2 kg",
            "first milestone is about 90.2 kg",
            "10 to 20 weeks",
            "ignores muscle, age and pregnancy",
        ),
    ),
    graph("target-02", "what is my ideal weight", PROGRESS, contains=("56.7 to 76.2 kg",)),
    graph("target-03", "what weight should I aim for", PROGRESS, contains=("56.7 to 76.2 kg",)),
    graph("target-04", "how much should I weigh", PROGRESS, contains=("56.7 to 76.2 kg",)),
    graph(
        "target-05",
        "what is a healthy weight for my height",
        PROGRESS,
        contains=("56.7 to 76.2 kg",),
    ),
    graph("target-06", "how much weight should I lose", PROGRESS, contains=("56.7 to 76.2 kg",)),
    graph(
        "target-07",
        "what should my target weight be",
        PROGRESS,
        body="B",
        contains=("60.0 to 80.6 kg", "BMI is 24.7", "no weight to lose"),
        avoids=("milestone",),
    ),
    graph(
        "target-08",
        "what should my target weight be",
        PROGRESS,
        body="C",
        contains=("BMI is 17.6", "50.4 to 67.7 kg", "won't suggest losing weight"),
        avoids=("milestone", "weeks"),
    ),
    graph(
        "target-09",
        "what should my target weight be",
        PROGRESS,
        body="D",
        contains=("BMI is 27.8", "no weight-loss target"),
        avoids=("milestone",),
    ),
    graph(
        "target-10",
        "what should my target weight be",
        PROGRESS,
        body="E",
        contains=("BMI is 50.8", "needs a doctor"),
        avoids=("milestone",),
    ),
    graph(
        "target-11",
        "what should my target weight be",
        PROGRESS,
        body="F",
        contains=("BMI is 22.1", "no weight target for a gain goal"),
        avoids=("milestone",),
    ),
    # --- questions that must KEEP their old route (negative controls)
    graph("keep-01", "what should i eat to lose weight", NUTRITION),
    graph("keep-02", "give me a high protein breakfast", NUTRITION),
    graph("keep-03", "plan my meals for the week", NUTRITION),
    graph("keep-04", "what workout should i do today", MOVEMENT),
    graph("keep-05", "should i do cardio or lift", MOVEMENT),
    graph(
        "mixed-01",
        "how is my weight and what should i eat",
        PROGRESS | NUTRITION,
        hist="weight_down",
        contains=("93.6 kg",),
    ),
    graph(
        "mixed-02",
        "how was my sleep and what workout should i do",
        PROGRESS | MOVEMENT,
        hist="sleep_ok",
        contains=("averaged 7.4 hours",),
    ),
    # --- unsafe requests are declined before any agent sees them (API pre-check, D108)
    screen("unsafe-01", "i want to weigh 40 kg, how do i get there?", "target_below_healthy"),
    screen("unsafe-02", "help me get down to 45 kg", "target_below_healthy"),
    screen("unsafe-03", "I want to be 100 lbs", "target_below_healthy"),
    screen("unsafe-04", "lose 10 kg in a week", "loss_too_fast"),
    screen("unsafe-05", "how can i drop 5 kg in 3 days", "loss_too_fast"),
    screen("unsafe-06", "how low can i go", "how_low"),
    screen("unsafe-07", "i want to weigh 52 kg", "target_below_healthy", body="B"),
    screen("safe-01", "i want to weigh 70 kg", None),
    screen("safe-02", "i want to lose 0.5 kg a week", None),
    screen("unsafe-08", "i want to weigh 50 kg", "target_below_healthy", body="C"),  # BMI 18.37
    screen("safe-03", "i want to weigh 52 kg", None, body="C"),  # BMI 19.1
    # --- HELD OUT: written once, never used to tune routing phrases or wording
    graph("held-01", "Where do I stand health-wise?", PROGRESS, "heldout"),
    graph("held-02", "how well have I been sleeping", PROGRESS, "heldout", hist="sleep_ok"),
    graph("held-03", "is my weight where it should be", PROGRESS, "heldout"),
    graph("held-04", "what's my body mass index", PROGRESS, "heldout", contains=("BMI is 31.0",)),
    graph("held-05", "what number should I be on the scales", PROGRESS, "heldout"),
    graph("held-06", "am I getting enough rest at night", PROGRESS, "heldout", hist="sleep_poor"),
    graph("held-07", "so... how am I doing overall", PROGRESS, "heldout"),
    graph("held-08", "How is my sleep going this month", PROGRESS, "heldout", hist="sleep_ok"),
    graph("held-09", "tell me about my weight", PROGRESS, "heldout"),
    graph("held-10", "should I weigh less", PROGRESS, "heldout"),
)

# ------------------------------------------------------------------------------------- running
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


@contextmanager
def every_model_down() -> Iterator[None]:
    original = ScriptedRouter.complete

    def down(self: ScriptedRouter, req: LLMRequest, max_wait_s: float = 0.0) -> LLMResponse:
        raise AllProvidersFailed(["groq: HTTP 429", "gemini: HTTP 503"])

    ScriptedRouter.complete = down  # type: ignore[method-assign]
    try:
        yield
    finally:
        ScriptedRouter.complete = original  # type: ignore[method-assign]


@dataclass
class Result:
    case: Case
    passed: bool
    problems: list[str]
    text: str = ""


def run_screen(case: Case) -> Result:
    body = BODIES[case.body]
    got = unsafe_weight_request(
        case.question, height_cm=body["height_cm"], weight_kg=body["weight_kg"]
    )
    ok = got == case.decline
    return Result(case, ok, [] if ok else [f"screen gave {got!r}, expected {case.decline!r}"])


def run_graph(case: Case) -> Result:
    bus, _router, _clock = world(body=BODIES[case.body], history=history(case.hist))
    out = ask(bus, "u-1", case.question, T0)
    problems: list[str] = []
    planned = next((s for s in out.events if s.meta.type == "run.planned"), None)
    routed = (
        frozenset(specialist_from_proto(s).value for s in planned.payload.specialists)
        if planned
        else frozenset()
    )
    if routed != case.route:
        problems.append(f"routed to {sorted(routed)}, expected {sorted(case.route or [])}")
    if out.final != "released":
        problems.append(f"not released ({out.final}) {list(out.errors)}")
        return Result(case, False, problems, out.text)
    released = next(s for s in out.events if s.meta.type == "recommendation.released")
    claim_text = " ".join(c.text for c in released.payload.claims)
    for n in _NUMBER.findall(out.text):
        if n not in claim_text:
            problems.append(f"the number {n} is in the answer but in no claim")
    low = out.text.lower()
    problems += [f"missing {s!r}" for s in case.contains if s.lower() not in low]
    problems += [f"must not contain {s!r}" for s in case.avoids if s.lower() in low]
    return Result(case, not problems, problems, out.text)


def run_all(group: str | None = None, only: Callable[[Case], bool] | None = None) -> list[Result]:
    results: list[Result] = []
    with every_model_down():
        for case in CASES:
            if group and case.group != group:
                continue
            if only and not only(case):
                continue
            results.append(run_screen(case) if case.screen else run_graph(case))
    return results


def summary(results: list[Result]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for g in ("tuning", "heldout"):
        rs = [r for r in results if r.case.group == g]
        if rs:
            out[g] = {"cases": len(rs), "passed": sum(r.passed for r in rs)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", choices=["tuning", "heldout"])
    ap.add_argument("--json", type=Path)
    ap.add_argument("--show", action="store_true", help="print every answer text")
    args = ap.parse_args()
    results = run_all(args.group)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    for r in results:
        mark = "ok  " if r.passed else "FAIL"
        print(f"{mark} {r.case.id:<12} {r.case.question[:52]}")
        for p in r.problems:
            print(f"       - {p}")
        if args.show and r.text:
            print(f"       > {r.text}")
    s = summary(results)
    print(json.dumps(s))
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "summary": s,
                    "results": [
                        {
                            "id": r.case.id,
                            "group": r.case.group,
                            "passed": r.passed,
                            "problems": r.problems,
                            "text": r.text,
                        }
                        for r in results
                    ],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
