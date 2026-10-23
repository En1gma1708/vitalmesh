"""What a user may see of a run, in plain words (D40, D74).

The tracker records every event with its internals (rule ids, tiers, specialists, model calls).
This module is the boundary between that and the person asking: a short list of steps in plain
language, a status, and, once released, the answer with the reason behind each statement. It never
returns rule ids, tier names, model call counts or why the safety check refused (the user is told
generically, D40). The operator gets the entries as they are from a separate, opt-in route.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

_PART = {
    "nutrition": "Food",
    "movement_recovery": "Training and sleep",
    "condition_medication": "Your conditions and labs",
}
_BASIS = {
    "computed": "Calculated from your profile and recent data",
    "cited": "Based on published guidance",
    "inferred": "A reasoned estimate from your data",
}
BLOCKED_TEXT = "I can't give an answer to this one. A doctor or dietitian can help."


class Step(BaseModel):
    at: str
    text: str


class Item(BaseModel):
    text: str
    basis: str


class Source(BaseModel):
    document: str
    locator: str
    licence: str
    url: str


class Answer(BaseModel):
    text: str
    partial_note: str | None
    items: list[Item]
    sources: list[Source]


class RunView(BaseModel):
    status: str  # running | released | needs_review | blocked
    steps: list[Step]
    answer: Answer | None


def _names(kinds: list[str]) -> str:
    return ", ".join(_PART.get(k, k) for k in kinds)


def step_text(entry: dict[str, Any]) -> str | None:
    """One plain sentence for an entry, or None if the user needs no line for it."""
    t = entry["type"]
    if t == "question.asked":
        return "Got your question."
    if t == "run.planned":
        if entry.get("tier") == "T1":
            return "Found a ready answer for people like you and personalised it with your numbers."
        if entry.get("tier") == "T0":
            return "The service is busy, so I'm using your own targets directly."
        return f"Asking the right helpers: {_names(entry.get('specialists', []))}."
    if t == "advice.completed":
        part = _PART.get(entry.get("specialist", ""), "A helper")
        return f"{part}: couldn't finish." if entry.get("status") == "failed" else f"{part}: done."
    if t == "advice.bundle.ready":
        if entry.get("partial"):
            return "Putting the answer together; one part is missing."
        return "Putting the answer together."
    if t == "recommendation.drafted":
        return "Rewriting the draft." if entry.get("revision", 0) >= 1 else "Draft ready."
    if t == "safety.cleared":
        return "Safety check passed."
    if t == "recommendation.blocked":
        return "I can't give this answer."
    if t == "review.completed":
        return {
            "approve": "An independent check agreed.",
            "revise": "An independent check asked for a rewrite.",
            "reject": "An independent check did not approve it.",
        }.get(entry.get("verdict", ""))
    if t == "review.requested":
        return "A clinician needs to look at this before you see it."
    if t == "review.decided":
        return (
            "A clinician approved it."
            if entry.get("decision") == "approve"
            else "A clinician did not approve it."
        )
    if t == "recommendation.released":
        return "Done."
    return None


def status_of(entries: list[dict[str, Any]]) -> str:
    status = "running"
    for e in entries:
        t = e["type"]
        if t == "recommendation.released":
            return "released"
        if t == "recommendation.blocked":
            return "blocked"
        if t == "review.requested":
            status = "needs_review"
        elif t == "review.decided" and e.get("decision") == "approve":
            status = "running"  # released follows
        elif t == "review.decided":
            status = "blocked"
    return status


def answer_of(entries: list[dict[str, Any]]) -> Answer | None:
    released = next((e for e in entries if e["type"] == "recommendation.released"), None)
    if released is None:
        return None
    missing = released.get("missing", [])
    return Answer(
        text=released.get("text", ""),
        partial_note=(
            f"This answer is partial: {_names(missing)} could not be included."
            if released.get("partial")
            else None
        ),
        items=[
            Item(text=c["text"], basis=_BASIS.get(c["basis"], "Not verified"))
            for c in released.get("claims", [])
        ],
        sources=[
            Source(document=c["document"], locator=c["locator"], licence=c["licence"], url=c["url"])
            for c in released.get("citations", [])
        ],
    )


def view_of(entries: list[dict[str, Any]]) -> RunView:
    steps = [Step(at=e["at"], text=text) for e in entries if (text := step_text(e)) is not None]
    status = status_of(entries)
    if status == "blocked" and not any(s.text == "I can't give this answer." for s in steps):
        steps.append(Step(at=entries[-1]["at"], text="I can't give this answer."))
    return RunView(status=status, steps=steps, answer=answer_of(entries))
