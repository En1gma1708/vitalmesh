"""The movement helper's workout tool (D101): concrete workouts, also with no model at all."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from vitalmesh_llm import AllProvidersFailed, LLMRequest, LLMResponse
from vitalmesh_specialists import MovementSpecialist
from vitalmesh_specialists.movement import (
    MovementTools,
    fallback_text,
    workout_args_from_question,
)
from vitalmesh_stores import InMemoryHistoryStore
from vitalmesh_wellness.engine import Profile
from vitalmesh_wellness.workouts import Place, plan_workout

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


class NoEvidence:
    def answer(self, question: str) -> Any:
        raise AssertionError("the workout path must not need the guideline search")


def profile(**extra: Any) -> Profile:
    base: dict[str, Any] = {
        "user_id": "t", "sex": "female", "age_years": 30, "weight_kg": 65, "height_cm": 165,
        "activity": "moderate", "resting_hr_bpm": 60, "goal": "maintain",
    }  # fmt: skip
    return Profile.model_validate({**base, **extra})


def tools(now: datetime = NOW, sleep: float = 8.0, **extra: Any) -> MovementTools:
    return MovementTools(profile(**extra), [sleep] * 14, InMemoryHistoryStore(), NoEvidence(), now)


def day_for(session: str) -> datetime:
    """A date at 09:00 UTC on which the gym rotation gives this session."""
    from vitalmesh_wellness.claims import UserConstraints

    u = UserConstraints(user_id="t", sex="female", age_years=30)
    for offset in range(8):
        d = date(2026, 10, 22) + timedelta(days=offset)
        if plan_workout(u, d, Place.GYM, 45).session == session:
            return datetime(d.year, d.month, d.day, 9, 0, tzinfo=UTC)
    raise AssertionError(session)


def call(t: MovementTools, **args: Any) -> dict[str, Any]:
    r = t.call("get_workout_plan", json.dumps(args))
    assert r.ok, r.error
    return dict(r.payload)


def texts(payload: dict[str, Any]) -> list[str]:
    return [c["text"] for c in payload["claims"]]


# ----------------------------------------------------------------------------- the tool
def test_the_tool_is_advertised_and_its_schema_is_closed() -> None:
    spec = next(s for s in tools().specs if s.name == "get_workout_plan")
    assert spec.parameters["additionalProperties"] is False
    assert spec.parameters["properties"]["place"]["enum"] == ["gym", "home", "either"]


def test_a_gym_push_day_lists_named_lifts_with_sets_and_reps() -> None:
    p = call(tools(now=day_for("push")), place="gym", minutes=45)
    t = texts(p)
    assert t[0].startswith("Today: push day")
    assert "Bench press: 3 sets of 8-12 reps." in t
    names = [e["name"] for e in p["exercises"]]
    assert names[0] == "Bench press"
    assert all(e["cue"] for e in p["exercises"])  # the model gets a one-line how-to for each
    assert all(c["source"].startswith("vitalmesh_wellness.workouts@") for c in p["claims"])


def test_a_cardio_day_gives_minutes_and_the_heart_rate_range() -> None:
    p = call(tools(now=day_for("cardio")), place="gym", minutes=30)
    t = texts(p)
    assert t[0].startswith("Today: 30 minutes of easy cardio")
    assert re_zone(t[1])
    assert p["exercises"] == []


def re_zone(text: str) -> bool:
    import re

    return re.fullmatch(r"Easy pace is zone 2: \d+-\d+ bpm\.", text) is not None


def test_either_adds_a_no_gym_option_on_a_strength_day() -> None:
    p = call(tools(now=day_for("push")), place="either", minutes=45)
    t = texts(p)
    assert any(x.startswith("Today: push day") for x in t)
    assert any(x.startswith("No gym: 45 minutes of easy cardio") for x in t)
    assert len(p["exercises"]) == 5  # only the gym session's exercises are listed


def test_either_on_a_cardio_day_is_not_duplicated() -> None:
    p = call(tools(now=day_for("cardio")), place="either", minutes=30)
    assert sum("easy cardio" in x for x in texts(p)) == 1
    assert not any(x.startswith("No gym:") for x in texts(p))


def test_home_gives_a_session_that_needs_no_gym() -> None:
    now = day_for("push")  # on a different rotation the home plan is full body or cardio
    p = call(tools(now=now), place="home", minutes=45)
    t = texts(p)
    assert any("at home" in x or "easy cardio" in x for x in t[:1])
    assert not any("Bench" in x or "gym" in x for x in t)


def test_a_condition_limits_it_to_easy_cardio_with_a_doctor_note() -> None:
    p = call(tools(now=day_for("push"), conditions={"cardiac"}), place="gym", minutes=45)
    assert p["exercises"] == []
    assert any("check with your doctor" in x for x in texts(p))
    assert not any("Bench" in x for x in texts(p))


def test_defaults_are_30_minutes_either_place() -> None:
    p = call(tools(now=day_for("cardio")))
    assert texts(p)[0].startswith("Today: 30 minutes")


@pytest.mark.parametrize(
    "args",
    [
        {"minutes": 9},
        {"minutes": 91},
        {"minutes": "thirty"},
        {"place": "moon"},
        {"focus": "bench_all_day"},
        {"unexpected": 1},
    ],
)
def test_bad_arguments_are_refused_not_guessed(args: dict[str, Any]) -> None:
    r = tools().call("get_workout_plan", json.dumps(args))
    assert not r.ok and r.error is not None and r.error.startswith("bad arguments")


def test_a_guardian_block_makes_the_tool_fail_closed() -> None:
    from vitalmesh_wellness.guardian import Guardian

    def never(_rec: Any) -> list[Any]:
        from vitalmesh_wellness.guardian import Violation

        return [Violation(rule="x", message="blocked for the test")]

    blocker = Guardian(rules=(never,))
    t = MovementTools(profile(), [8.0] * 14, InMemoryHistoryStore(), NoEvidence(), NOW, blocker)
    r = t.call("get_workout_plan", "{}")
    assert not r.ok and r.error == "blocked by safety rules"


def test_every_workout_claim_is_a_computed_number_for_the_checks_downstream() -> None:
    p = call(tools(now=day_for("push")), place="gym", minutes=45)
    metrics = {c["metric"] for c in p["claims"]}
    assert metrics == {"workout_minutes", "workout_exercise_sets", "workout_total_sets"}
    assert all(c["unit"] in ("min", "sets", "zone") for c in p["claims"])


# ----------------------------------------------------------------------------- no model
@pytest.mark.parametrize(
    "question",
    [
        "what workout should I do",
        "should i do cardio or gym?",
        "I want to build muscle, what should I do?",
        "Is it okay to exercise when I slept badly?",
        "push day today?",
        "can I run tomorrow",
    ],
)
def test_a_workout_question_adds_the_workout_call(question: str) -> None:
    names = [n for n, _ in tools().default_calls(question)]
    assert names == ["get_training_targets", "get_activity_summary", "get_workout_plan"]


@pytest.mark.parametrize(
    "question", ["How much should I sleep?", "How many calories should I eat?", "hello"]
)
def test_other_questions_keep_the_two_original_calls(question: str) -> None:
    names = [n for n, _ in tools().default_calls(question)]
    assert names == ["get_training_targets", "get_activity_summary"]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("what workout should I do", {"place": "either"}),
        ("workout at home please", {"place": "home"}),
        ("I can't go to the gym today, what workout", {"place": "home"}),
        ("no gym, what exercise", {"place": "home"}),
        ("30 minutes workout", {"place": "either", "minutes": 30}),
        ("a 45 min push workout", {"place": "either", "minutes": 45, "focus": "push"}),
        ("leg day?", {"place": "either", "focus": "legs"}),
        ("a pull session", {"place": "either", "focus": "pull"}),
        ("full body workout at home", {"place": "home", "focus": "full_body"}),
        ("5 minutes workout", {"place": "either"}),  # outside the allowed range: ignored
        ("200 minutes workout", {"place": "either"}),
    ],
)
def test_the_question_is_read_for_place_minutes_and_focus(
    question: str, expected: dict[str, Any]
) -> None:
    assert workout_args_from_question(question) == expected


def test_the_chosen_arguments_always_pass_the_tool_validation() -> None:
    for q in ("push 90 minutes at home", "workout 10 mins", "no equipment, pull workout"):
        r = tools().call("get_workout_plan", json.dumps(workout_args_from_question(q)))
        assert r.ok, (q, r.error)


class DownRouter:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, req: LLMRequest, max_wait_s: float = 0.0) -> LLMResponse:
        self.calls += 1
        raise AllProvidersFailed(["groq: HTTP 429", "gemini: HTTP 503"])


def test_with_no_model_the_helper_still_answers_with_a_concrete_workout() -> None:
    router = DownRouter()
    specialist = MovementSpecialist(router, attempts=1, sleep=lambda _: None)
    advice = specialist.advise_or_degrade("what workout should I do", tools(now=day_for("push")))
    assert advice.status == "fallback"
    assert advice.model_calls == 0
    assert "Bench press: 3 sets of 8-12 reps." in advice.text
    assert "Today: push day" in advice.text
    assert any(r.name == "get_workout_plan" and r.ok for r in advice.results)


def test_the_fallback_text_includes_the_workout_claims() -> None:
    p = tools(now=day_for("cardio")).call("get_workout_plan", json.dumps({"minutes": 30}))
    assert "easy cardio" in fallback_text([p])


# ----------------------------------------------------------------------------- D104: history
def with_log(
    log: Any, now: datetime = NOW, history: InMemoryHistoryStore | None = None, **extra: Any
) -> MovementTools:
    return MovementTools(
        profile(**extra), [8.0] * 14, history or InMemoryHistoryStore(), NoEvidence(), now,
        workouts=log,
    )  # fmt: skip


def test_the_suggested_session_is_remembered() -> None:
    from vitalmesh_stores import InMemoryWorkoutLog

    log = InMemoryWorkoutLog()
    now = day_for("push")
    call(with_log(log, now), place="gym", minutes=45)
    assert log.recent("t") == {now.date(): "push"}


def test_yesterdays_session_is_not_repeated() -> None:
    from vitalmesh_stores import InMemoryWorkoutLog

    log = InMemoryWorkoutLog()
    now = day_for("push")
    log.record("t", now.date() - timedelta(days=1), "push")
    p = call(with_log(log, now), place="gym", minutes=45)
    assert not texts(p)[0].startswith("Today: push day")


def test_a_session_from_three_days_ago_no_longer_counts() -> None:
    from vitalmesh_stores import InMemoryWorkoutLog

    log = InMemoryWorkoutLog()
    now = day_for("push")
    log.record("t", now.date() - timedelta(days=3), "push")
    p = call(with_log(log, now), place="gym", minutes=45)
    assert texts(p)[0].startswith("Today: push day")


def test_asking_twice_the_same_day_gives_the_same_workout() -> None:
    from vitalmesh_stores import InMemoryWorkoutLog

    log = InMemoryWorkoutLog()
    now = day_for("push")
    t = with_log(log, now)
    assert texts(call(t, place="gym", minutes=45)) == texts(call(t, place="gym", minutes=45))


def test_a_log_outage_does_not_stop_the_answer() -> None:
    class Broken:
        def recent(self, user_id: str) -> Any:
            raise RuntimeError("down")

        def record(self, *a: Any) -> None:
            raise RuntimeError("down")

    p = call(with_log(Broken(), day_for("push")), place="gym", minutes=45)
    assert texts(p)[0].startswith("Today: push day")


def test_a_blocked_plan_is_not_remembered() -> None:
    from vitalmesh_stores import InMemoryWorkoutLog
    from vitalmesh_wellness.guardian import Guardian, Violation

    def never(_rec: Any) -> list[Any]:
        return [Violation(rule="x", message="blocked")]

    log = InMemoryWorkoutLog()
    t = MovementTools(
        profile(), [8.0] * 14, InMemoryHistoryStore(), NoEvidence(), NOW, Guardian(rules=(never,)),
        workouts=log,
    )  # fmt: skip
    assert not t.call("get_workout_plan", "{}").ok
    assert log.recent("t") == {}


def _active(
    history: InMemoryHistoryStore, now: datetime, minutes_by_days_ago: dict[int, float]
) -> None:
    from vitalmesh_stores import Point

    for ago, value in minutes_by_days_ago.items():
        history.add("t", "active_minutes", Point(now - timedelta(days=ago), value, "min"))


def test_a_very_inactive_week_eases_the_session_in() -> None:
    now = day_for("push")
    h = InMemoryHistoryStore()
    _active(h, now, {2: 10, 3: 15, 20: 200})  # 25 minutes this week
    p = call(with_log(None, now, h), place="gym", minutes=60)
    assert len(p["exercises"]) == 3
    assert any("starts light" in x for x in texts(p))


def test_an_active_week_or_no_data_does_not() -> None:
    now = day_for("push")
    h = InMemoryHistoryStore()
    _active(h, now, {1: 40, 3: 40})  # 80 minutes: at or over the threshold
    assert len(call(with_log(None, now, h), place="gym", minutes=60)["exercises"]) == 5
    assert len(call(with_log(None, now), place="gym", minutes=60)["exercises"]) == 5  # no data
    h2 = InMemoryHistoryStore()
    _active(h2, now, {1: 30, 2: 29})  # 59: just under -> eased
    assert len(call(with_log(None, now, h2), place="gym", minutes=60)["exercises"]) == 3
    h3 = InMemoryHistoryStore()
    _active(h3, now, {1: 30, 2: 30})  # exactly 60: not eased
    assert len(call(with_log(None, now, h3), place="gym", minutes=60)["exercises"]) == 5


def test_a_history_outage_means_no_ease_in() -> None:
    class Broken:
        def series(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("down")

    now = day_for("push")
    t = MovementTools(profile(), [8.0] * 14, Broken(), NoEvidence(), now)  # type: ignore[arg-type]
    assert t.call("get_workout_plan", json.dumps({"place": "gym", "minutes": 60})).ok


def test_the_movement_text_leads_with_the_concrete_workout() -> None:
    t = tools(now=day_for("push"))
    results = [
        t.call("get_training_targets", "{}"),
        t.call("get_activity_summary", "{}"),
        t.call("get_workout_plan", json.dumps({"place": "gym", "minutes": 45})),
    ]
    text = fallback_text(results)
    assert text.startswith("Today: push day")
    assert text.index("Today: push day") < text.index("Aim for")  # workout first


def test_the_no_gym_option_is_one_sentence_so_a_cut_cannot_leave_a_stub() -> None:
    import re

    p = call(tools(now=day_for("push")), place="either", minutes=45)
    alt = next(x for x in texts(p) if x.startswith("No gym"))
    assert len(re.split(r"(?<=[.!?])\s+", alt.strip())) == 1
    assert "?" not in alt


def test_a_workout_replaces_the_general_zone_sentence_but_not_the_claim() -> None:
    t = tools(now=day_for("cardio"))
    results = [
        t.call("get_training_targets", "{}"),
        t.call("get_workout_plan", json.dumps({"place": "gym", "minutes": 30})),
    ]
    text = fallback_text(results)
    assert "Train in zone" not in text and "Easy pace is zone 2" in text
    assert any(
        c["metric"] == "target_hr_zone" for c in results[0].payload["claims"]
    )  # still on record
    without = fallback_text(results[:1])
    assert "Train in zone" in without  # no workout: the zone line is the answer
