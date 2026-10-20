from __future__ import annotations

from datetime import UTC, datetime

from vitalmesh_wellness.synthetic import synthetic_history, synthetic_user

END = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
BOUNDS = {
    "body_weight": (20, 400),
    "sleep_duration": (0, 24),
    "active_minutes": (0, 1440),
    "step_count": (0, 200_000),
    "energy_intake": (0, 20_000),
}  # the API's plausibility ranges, restated here so the generator cannot drift out of them


def test_history_is_deterministic_daily_and_in_range() -> None:
    for i in range(50):
        profile, _ = synthetic_user(9, i)
        h = synthetic_history(9, profile, 60, END)
        assert h == synthetic_history(9, profile, 60, END)
        for metric, pts in h.items():
            assert len(pts) == 60
            assert [p[0] for p in pts] == sorted(p[0] for p in pts)
            low, high = BOUNDS[metric]
            assert all(low <= v <= high for _, v in pts), metric
        assert h["body_weight"][-1][0] == END


def test_different_users_get_different_histories() -> None:
    a, _ = synthetic_user(9, 1)
    b, _ = synthetic_user(9, 2)
    assert synthetic_history(9, a, 30, END) != synthetic_history(9, b, 30, END)
