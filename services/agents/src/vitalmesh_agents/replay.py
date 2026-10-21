"""Counterfactual replay in shadow mode (D39 item 5, D68).

Take questions that were really asked, run each through two configurations of the graph (the
baseline and a candidate: a new prompt, model alias or rule) on the in-process bus, and diff the
outcomes. Nothing is published to Kafka or shown to a user: the candidate runs in the shadows, so a
change is judged on past traffic before it ships. This is the use of an event log that direct
agent-to-agent calls cannot give.

What a diff means: it says WHAT CHANGED (the final state, the checker verdict, the claims, the
specialists, the text), not whether the change is better. Reading the diffs is a human decision;
the counts only say where to look.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from vitalmesh_agents.inprocess import Bus, Outcome, ask


@dataclass(frozen=True)
class Question:
    user_id: str
    text: str


@dataclass(frozen=True)
class Replayed:
    question: Question
    baseline: Outcome
    candidate: Outcome
    diffs: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.diffs)


def _types(o: Outcome, type_: str) -> list[object]:
    return [s.payload for s in o.events if s.meta.type == type_]


def _verdicts(o: Outcome) -> list[int]:
    return [p.verdict for p in _types(o, "review.completed")]  # type: ignore[attr-defined]


def _specialists(o: Outcome) -> set[int]:
    plans = _types(o, "run.planned")
    return {int(s) for p in plans for s in p.specialists}  # type: ignore[attr-defined]


def _claim_texts(o: Outcome) -> set[str]:
    out: set[str] = set()
    for p in _types(o, "advice.bundle.ready"):
        for part in p.parts:  # type: ignore[attr-defined]
            out |= {c.text for c in part.claims}
    return out


def diff(a: Outcome, b: Outcome) -> tuple[str, ...]:
    out: list[str] = []
    if a.final != b.final:
        out.append(f"final: {a.final} -> {b.final}")
    if _specialists(a) != _specialists(b):
        out.append("specialists differ")
    if _claim_texts(a) != _claim_texts(b):
        out.append("claims differ")
    if _verdicts(a) != _verdicts(b):
        out.append(f"checker verdicts: {_verdicts(a)} -> {_verdicts(b)}")
    if a.text != b.text:
        out.append("reply text differs")
    if b.errors and not a.errors:
        out.append(f"candidate raised: {b.errors[0][:80]}")
    return tuple(out)


def replay(
    questions: Iterable[Question],
    baseline: Callable[[], Bus],
    candidate: Callable[[], Bus],
) -> list[Replayed]:
    """Each question runs on a FRESH bus per configuration so runs cannot influence each other."""
    out: list[Replayed] = []
    for q in questions:
        a = ask(baseline(), q.user_id, q.text)
        b = ask(candidate(), q.user_id, q.text)
        out.append(Replayed(q, a, b, diff(a, b)))
    return out


def summary(results: list[Replayed]) -> str:
    changed = [r for r in results if r.changed]
    lines = [f"{len(changed)} of {len(results)} questions changed"]
    for r in changed:
        lines.append(f"  {r.question.text[:60]!r}: " + "; ".join(r.diffs))
    return "\n".join(lines)


def questions_from_events(events: Iterable[tuple[str, str]]) -> list[Question]:
    """From (user_id, question) pairs, e.g. read back from ``questions.asked``; repeats removed."""
    seen: set[tuple[str, str]] = set()
    out: list[Question] = []
    for user_id, text in events:
        if (user_id, text) not in seen:
            seen.add((user_id, text))
            out.append(Question(user_id, text))
    return out
