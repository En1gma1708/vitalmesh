"""The Progress & Status helper (D108): numbers from calculators, honest about missing data,
no target where none is safe, and a full answer with no model at all."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from vitalmesh_llm import AllProvidersFailed, LLMRequest, LLMResponse
from vitalmesh_specialists import ProgressSpecialist, ProgressTools
from vitalmesh_specialists.progress import fallback_text
from vitalmesh_stores import InMemoryHistoryStore, Point
from vitalmesh_wellness.engine import Profile

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


def profile(**extra: Any) -> Profile:
    return Profile.model_validate(
        {
            "user_id": "t", "sex": "female", "age_years": 35, "weight_kg": 95, "height_cm": 175,
            "activity": "moderate", "resting_hr_bpm": 60, "goal": "lose",
            **extra,
        }
    )  # fmt: skip


def add(history: InMemoryHistoryStore, metric: str, values: list[float], unit: str) -> None:
    n = len(values)
    for i, v in enumerate(values):
        history.add("t", metric, Point(t=NOW - timedelta(days=n - 1 - i), value=v, unit=unit))


def tools(history: InMemoryHistoryStore | None = None, **extra: Any) -> ProgressTools:
    return ProgressTools(profile(**extra), history or InMemoryHistoryStore(), NOW)


def run(t: ProgressTools, name: str) -> dict[str, Any]:
    r = t.call(name, "{}")
    assert r.ok, r.error
    return {**r.payload, "_meta": r.meta}


def texts(payload: dict[str, Any]) -> list[str]:
    return [c["text"] for c in payload["claims"]]


def metrics(payload: dict[str, Any]) -> dict[str, float]:
    return {c["metric"]: c["value"] for c in payload["claims"]}


# ------------------------------------------------------------------------------ body metrics
def test_bmi_and_the_healthy_range_come_from_the_calculators() -> None:
    p = run(tools(), "get_body_metrics")  # 95 kg, 175 cm: BMI 31.0
    m = metrics(p)
    assert m["bmi"] == 31.0 and m["healthy_weight_low"] == 56.7 and m["healthy_weight_high"] == 76.2
    assert any("BMI is 31.0" in t and "obesity" in t for t in texts(p))
    assert any("56.7 to 76.2 kg" in t for t in texts(p))


def test_a_person_who_chose_to_lose_gets_a_gradual_milestone_and_a_pace() -> None:
    p = run(tools(), "get_body_metrics")
    m = metrics(p)
    assert m["weight_milestone"] == 90.2 and m["weight_milestone_bmi"] == 29.5
    assert m["weight_loss_rate"] == 0.5 and m["weight_loss_rate_pct"] == 0.53
    assert any("10 to 20 weeks" in t for t in texts(p))


def test_the_milestone_never_gives_a_bmi_under_the_underweight_line() -> None:
    for weight in range(60, 200, 5):
        r = tools(weight_kg=weight, goal="lose").call("get_body_metrics", "{}")
        assert r.ok, (weight, r.error)  # the Guardian inside the tool cleared it
        for c in r.payload["claims"]:
            if c["metric"] == "weight_milestone_bmi":
                assert c["value"] >= 18.5


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ({"goal": "maintain"}, "set your goal to lose weight"),
        ({"goal": "gain"}, "no weight target for a gain goal"),
        ({"weight_kg": 50, "goal": "lose"}, "won't suggest losing weight"),
        ({"age_years": 70}, "no weight-loss target"),
        ({"weight_kg": 70, "goal": "lose"}, "no weight to lose"),
        ({"weight_kg": 140, "goal": "lose"}, "needs a doctor"),  # BMI 45.7
        ({"weight_kg": 45, "goal": "lose"}, "needs a doctor"),  # BMI 14.7
    ],
)
def test_no_milestone_where_none_is_safe(extra: dict[str, Any], needle: str) -> None:
    p = run(tools(**extra), "get_body_metrics")
    assert "weight_milestone" not in metrics(p)
    assert "weight_loss_rate" not in metrics(p)
    assert any(needle in n for n in p["notes"]), p["notes"]


def test_the_bmi_caveat_and_the_bands_are_always_present() -> None:
    p = run(tools(), "get_body_metrics")
    assert any("ignores muscle, age and pregnancy" in n for n in p["notes"])
    cited = {c["doc_id"] for c in p["_meta"]["citations"]}
    assert "nhlbi-obesity-1998" in cited
    assert any("18.5" in s for s in p["references"])


def test_a_declared_condition_adds_the_see_a_doctor_note_to_a_loss_milestone() -> None:
    p = run(tools(conditions=["hypertension"]), "get_body_metrics")
    assert "weight_milestone" in metrics(p)
    assert any("talk to your doctor before starting a weight-loss plan" in n for n in p["notes"])


# ----------------------------------------------------------------------------------- weight
def test_with_no_readings_it_says_so_and_uses_the_profile_weight_only_as_such() -> None:
    p = run(tools(), "get_weight_review")
    assert "You have not logged a weight yet" in texts(p)[0] and "95.0 kg" in texts(p)[0]
    assert not any("trend is" in t for t in texts(p))
    assert any("Log your weight" in n for n in p["notes"])


def test_two_readings_are_not_a_trend() -> None:
    h = InMemoryHistoryStore()
    add(h, "body_weight", [90.0, 89.0], "kg")
    p = run(tools(h), "get_weight_review")
    assert "weight_trend_rate" not in metrics(p)
    assert any("2 weight readings" in t and "at least 3" in t for t in texts(p))
    assert any("Not enough readings" in n for n in p["notes"])


def test_a_real_trend_states_rate_changes_and_the_number_of_readings() -> None:
    h = InMemoryHistoryStore()
    add(h, "body_weight", [95 - 0.1 * i for i in range(15)], "kg")  # 0.7 kg a week down
    p = run(tools(h), "get_weight_review")
    m = metrics(p)
    assert m["body_weight"] == pytest.approx(93.6) and m["weight_trend_rate"] == pytest.approx(-0.7)
    assert m["weight_change_7d"] == pytest.approx(-0.7)
    assert "weight_change_30d" not in m  # 14 days of data do not cover half of 30
    assert any("from 15 readings" in t for t in texts(p))
    assert any("0.7 kg a week down" in t for t in texts(p))
    assert any("Weight status: on track" in n for n in p["notes"])


def test_a_stale_reading_is_flagged_with_its_age() -> None:
    h = InMemoryHistoryStore()
    for i in range(4):
        h.add("t", "body_weight", Point(t=NOW - timedelta(days=30 + 3 * i), value=90, unit="kg"))
    p = run(tools(h), "get_weight_review")
    assert any("23" in t or "30 days ago" in t for t in texts(p))
    assert any("more than two weeks old" in n for n in p["notes"])


def test_a_loss_faster_than_one_percent_a_week_is_called_out() -> None:
    h = InMemoryHistoryStore()
    add(h, "body_weight", [95 - 0.25 * i for i in range(15)], "kg")  # -1.75 kg a week
    p = run(tools(h), "get_weight_review")
    assert any("faster than is recommended" in n for n in p["notes"])
    assert any("Weight status: needs attention" in n for n in p["notes"])


# ------------------------------------------------------------------------------------ sleep
def test_with_too_few_nights_it_says_so() -> None:
    h = InMemoryHistoryStore()
    add(h, "sleep_duration", [7.0, 8.0], "h")
    p = run(tools(h), "get_sleep_review")
    assert "average_sleep_7d" not in metrics(p)
    assert any("I need at least 3" in t for t in texts(p))
    assert any("Sleep status: not enough data" in n for n in p["notes"])


def test_the_stored_neutral_sleep_value_is_never_used_as_data() -> None:
    p = run(tools(), "get_sleep_review")  # no history at all
    assert "average_sleep_7d" not in metrics(p)


def test_a_week_of_sleep_gets_average_nights_under_and_the_change_and_the_cited_guidance() -> None:
    h = InMemoryHistoryStore()
    add(h, "sleep_duration", [6.0] * 7 + [7.0, 8.0, 6.5, 7.5, 6.0, 8.0, 7.0], "h")
    p = run(tools(h), "get_sleep_review")
    m = metrics(p)
    assert m["average_sleep_7d"] == 7.1 and m["sleep_nights_below_min"] == 2
    assert m["sleep_change_vs_previous_week"] == 1.1
    assert any("1.1 hours a night more than the week before" in t for t in texts(p))
    assert p["_meta"]["citations"][0]["doc_id"] == "nhlbi-sleep-2011"
    assert p["_meta"]["citations"][0]["quote"].startswith("adults need at least 7")
    assert any("7\u20138 hours" in s for s in p["references"])
    assert any("Sleep status: on track" in n for n in p["notes"])


# ---------------------------------------------------------------------------------- overview
def test_the_overview_gives_each_area_a_status_and_only_checked_claims() -> None:
    h = InMemoryHistoryStore()
    add(h, "sleep_duration", [6.0, 6.5, 6.0, 6.0, 5.5, 6.0, 6.5], "h")
    add(h, "active_minutes", [30, 20, 40, 0, 35, 25, 10], "min")
    add(h, "body_weight", [94.0, 93.8, 93.6, 93.4, 93.2, 93.0, 92.8, 92.6], "kg")
    p = run(tools(h), "get_status_overview")
    m = metrics(p)
    assert m["average_sleep_7d"] == 6.1 and m["active_minutes_7d"] == 160 and m["bmi"] > 25
    status_line = next(n for n in p["notes"] if n.startswith("Sleep:"))
    assert "Sleep: needs attention" in status_line and "Activity: on track" in status_line


def test_the_overview_with_no_data_is_honest() -> None:
    p = run(tools(), "get_status_overview")
    line = next(n for n in p["notes"] if n.startswith("Sleep:"))
    assert "Sleep: not enough data" in line and "Activity: not enough data" in line


# ------------------------------------------------------------------------- tool-call surface
@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("how am i doing", ["get_status_overview"]),
        ("how was my sleep this week", ["get_sleep_review"]),
        ("how is my weight", ["get_weight_review", "get_body_metrics"]),
        ("what should my target weight be", ["get_weight_review", "get_body_metrics"]),
        ("what is my bmi", ["get_weight_review", "get_body_metrics"]),
        (
            "how is my sleep and my weight",
            ["get_sleep_review", "get_weight_review", "get_body_metrics"],
        ),
    ],
)
def test_the_model_free_route_picks_tools_from_the_question(
    question: str, expected: list[str]
) -> None:
    assert [n for n, _ in tools().default_calls(question)] == expected


def test_bad_arguments_and_unknown_tools_are_errors_not_exceptions() -> None:
    t = tools()
    assert not t.call("get_body_metrics", '{"weight": 50}').ok
    assert not t.call("get_body_metrics", "[1]").ok
    assert not t.call("get_body_metrics", "not json").ok
    assert not t.call("delete_everything", "{}").ok


def test_every_tool_runs_and_its_claims_clear_the_guardian() -> None:
    for name in (
        "get_status_overview",
        "get_weight_review",
        "get_sleep_review",
        "get_body_metrics",
    ):
        assert tools().call(name, "{}").ok


def test_a_history_outage_does_not_stop_the_answer() -> None:
    class Broken:
        def add(self, *a: Any) -> None: ...

        def series(self, *a: Any, **k: Any) -> list[Point]:
            raise ConnectionError("down")

    r = ProgressTools(profile(), Broken(), NOW).call("get_status_overview", "{}")
    assert r.ok


# ---------------------------------------------------------------------------- no model at all
class DownRouter:
    def complete(self, req: LLMRequest, max_wait_s: float = 0.0) -> LLMResponse:
        raise AllProvidersFailed(["groq: HTTP 429"])


def no_sleep(_: float) -> None:
    return None


def test_with_every_model_down_the_helper_still_answers_from_its_tools() -> None:
    h = InMemoryHistoryStore()
    add(h, "sleep_duration", [7.5, 8.0, 7.0, 7.0, 8.0, 7.5, 7.0], "h")
    helper = ProgressSpecialist(DownRouter(), attempts=1, sleep=no_sleep)
    advice = helper.advise_or_degrade("how was my sleep?", tools(h))
    assert advice.status == "fallback" and advice.model_calls == 0
    assert "averaged 7.4 hours" in advice.text
    assert "7\u20138 hours" in advice.text  # the cited guidance
    assert "Sleep status: on track" in advice.text


def test_every_number_in_the_fallback_text_is_in_a_claim_or_reference() -> None:
    h = InMemoryHistoryStore()
    add(h, "body_weight", [95 - 0.1 * i for i in range(15)], "kg")
    t = tools(h)
    results = [t.call(n, "{}") for n in ("get_weight_review", "get_body_metrics")]
    allowed: set[str] = set()
    for r in results:
        for c in r.payload["claims"]:
            allowed |= set(re.findall(r"\d+(?:\.\d+)?", c["text"]))
        for s in r.payload["references"]:
            allowed |= set(re.findall(r"\d+(?:\.\d+)?", s))
    said = set(re.findall(r"\d+(?:\.\d+)?", fallback_text(results)))
    assert said <= allowed, said - allowed


def test_the_fallback_has_no_ideal_weight_and_one_range() -> None:
    p = fallback_text([tools().call("get_body_metrics", "{}")])
    assert "ideal" not in p.lower()
    assert "56.7 to 76.2 kg" in p and p.count("76.2") == 1


def test_nothing_ok_gives_the_plain_pointer() -> None:
    assert "could not find checked information" in fallback_text([])


# ------------------------------------------------------- the scope note must not steer routing
def test_the_scope_note_a_handler_appends_does_not_change_which_tools_run() -> None:
    from vitalmesh_specialists.agent import SCOPE_MARKER, strip_scope

    asked = "what should my target weight be"
    scoped = (
        f"{asked}{SCOPE_MARKER}everyday progress, sleep, weight and BMI specialist. Answer only."
    )
    assert strip_scope(scoped) == asked and strip_scope(asked) == asked
    # the note names sleep and weight; it must not add the sleep tool to a weight question
    helper = ProgressSpecialist(DownRouter(), attempts=1, sleep=no_sleep)
    advice = helper.advise_or_degrade(scoped, tools())
    assert set(advice.tools_used) == {"get_weight_review", "get_body_metrics"}
