"""The scorecard (D108): statuses from the shared calculators, honest about missing data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from vitalmesh_api.status import build_status
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


def add(h: InMemoryHistoryStore, metric: str, values: list[float], unit: str) -> None:
    n = len(values)
    for i, v in enumerate(values):
        h.add("t", metric, Point(t=NOW - timedelta(days=n - 1 - i), value=v, unit=unit))


def areas(out: Any) -> dict[str, Any]:
    return {a.key: a for a in out.areas}


def test_with_no_data_every_area_says_so_and_asks_for_the_right_thing() -> None:
    out = build_status(profile(), InMemoryHistoryStore(), NOW, 2000)
    a = areas(out)
    assert [x.key for x in out.areas] == ["sleep", "weight", "activity", "intake"]
    assert a["sleep"].status == "not_enough_data" and "log 3 nights" in a["sleep"].detail
    assert a["activity"].status == "not_enough_data" and a["intake"].status == "not_enough_data"
    # weight always has the profile value and BMI, labelled as such by its detail
    assert a["weight"].headline == "95.0 kg" and "BMI 31.0" in a["weight"].detail
    assert "for a trend" in a["weight"].detail
    assert out.caveat.startswith("BMI is a rough guide")


def test_statuses_follow_the_shared_thresholds() -> None:
    h = InMemoryHistoryStore()
    add(h, "sleep_duration", [6.0, 6.5, 6.0, 6.0, 5.5, 6.0, 6.5], "h")
    add(h, "active_minutes", [30, 20, 40, 0, 35, 25, 10], "min")
    add(h, "energy_intake", [2000, 2100, 1900, 2050, 1950, 2000, 2000], "kcal")
    add(h, "body_weight", [94.0, 93.9, 93.8, 93.7, 93.6, 93.5, 93.4, 93.3], "kg")
    a = areas(build_status(profile(), h, NOW, 2000))
    assert a["sleep"].status == "needs_attention" and a["sleep"].headline == "6.1 h"
    assert a["activity"].status == "on_track" and a["activity"].headline == "160 min"
    assert a["intake"].status == "on_track" and a["intake"].headline == "2,000 kcal"
    assert a["weight"].status == "on_track" and a["weight"].headline == "93.3 kg"
    assert "down" in a["weight"].detail and "kg a week" in a["weight"].detail


def test_every_status_has_words_not_only_a_colour() -> None:
    out = build_status(profile(), InMemoryHistoryStore(), NOW, 2000)
    for area in out.areas:
        assert area.label in {"On track", "Needs attention", "Not enough data"}
        assert area.headline and area.detail


def test_a_stale_weight_says_how_old_it_is() -> None:
    h = InMemoryHistoryStore()
    for i in range(4):
        h.add("t", "body_weight", Point(t=NOW - timedelta(days=30 + 3 * i), value=90, unit="kg"))
    a = areas(build_status(profile(), h, NOW, 2000))
    assert "last reading" in a["weight"].detail and "days ago" in a["weight"].detail


def test_a_history_outage_is_not_an_error_page() -> None:
    class Broken:
        def add(self, *a: Any) -> None: ...

        def series(self, *a: Any, **k: Any) -> list[Point]:
            raise ConnectionError("down")

    out = build_status(profile(), Broken(), NOW, 2000)
    assert areas(out)["sleep"].status == "not_enough_data"
    assert build_status(profile(), None, NOW, None).areas  # no history store at all


def test_the_scorecard_agrees_with_the_progress_helper() -> None:
    """Same calculators, same thresholds: the page and a typed answer cannot disagree."""
    from vitalmesh_specialists import ProgressTools

    h = InMemoryHistoryStore()
    add(h, "sleep_duration", [7.5, 8.0, 7.0, 7.0, 8.0, 7.5, 7.0], "h")
    page = areas(build_status(profile(), h, NOW, 2000))["sleep"]
    answer = ProgressTools(profile(), h, NOW).call("get_sleep_review", "{}")
    assert page.status == "on_track" and "Sleep status: on track." in answer.payload["notes"]
    assert page.headline == "7.4 h" and "averaged 7.4 hours" in answer.payload["claims"][0]["text"]
