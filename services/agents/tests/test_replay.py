from __future__ import annotations

from vitalmesh_agents.inprocess import Bus
from vitalmesh_agents.replay import Question, diff, questions_from_events, replay, summary
from vitalmesh_agents.testing import ScriptedRouter, world


def bus_with(verdicts: list[str] | None = None, conditions: list[str] | None = None) -> Bus:
    return world(conditions=conditions, verdicts=verdicts)[0]


def test_identical_configurations_show_no_difference() -> None:
    qs = [Question("u-1", "What should I eat and how should I train?")]
    out = replay(qs, bus_with, bus_with)
    assert len(out) == 1 and not out[0].changed
    assert summary(out) == "0 of 1 questions changed"


def test_a_stricter_checker_in_the_candidate_is_visible_as_a_changed_outcome() -> None:
    qs = [Question("u-1", "What should I eat?")]
    out = replay(
        qs,
        lambda: bus_with(["approve"], ["celiac_disease"]),
        lambda: bus_with(["revise", "revise"], ["celiac_disease"]),
    )
    (r,) = out
    assert r.baseline.final == "released" and r.candidate.final == "review_requested"
    assert any(d.startswith("final:") for d in r.diffs)
    assert any(d.startswith("checker verdicts") for d in r.diffs)
    assert "1 of 1 questions changed" in summary(out)


def test_runs_do_not_influence_each_other() -> None:
    q = Question("u-1", "What should I eat?")
    first = replay([q], bus_with, bus_with)[0]
    second = replay([q], bus_with, bus_with)[0]
    assert first.baseline.final == second.baseline.final == "released"


def test_questions_are_deduplicated_in_order() -> None:
    qs = questions_from_events([("a", "q1"), ("b", "q1"), ("a", "q1"), ("a", "q2")])
    assert [(q.user_id, q.text) for q in qs] == [("a", "q1"), ("b", "q1"), ("a", "q2")]


def test_diff_of_a_run_with_itself_is_empty() -> None:
    bus = bus_with()
    from vitalmesh_agents.inprocess import ask

    o = ask(bus, "u-1", "What should I eat?")
    assert diff(o, o) == ()
    assert ScriptedRouter().purposes == []
