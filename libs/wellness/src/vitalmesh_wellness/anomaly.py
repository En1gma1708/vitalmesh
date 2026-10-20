"""Statistical anomaly detection stub (D53). Pure, deterministic, no model.

Flags the newest value of a series when it is far from the recent baseline. Two conditions must
both hold, so noise cannot trigger it on a very steady series and a large-but-normal swing in a
noisy series is not flagged:

* a robust z-score above ``Z_LIMIT``: ``0.6745 * (x - median) / MAD`` over the earlier values
  (Iglewicz and Hoaglin's modified z-score; the usual cut-off is 3.5);
* an absolute change of at least the metric's ``min_change`` (a guard so MAD near zero cannot
  turn a tiny change into an alarm).

This only *detects*; it never advises. Triage (later phase) decides what to do with a flag, and
any resulting advice still goes through the Guardian.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

ANOMALY_VERSION = "anomaly-1"
Z_LIMIT = 3.5
MIN_BASELINE_POINTS = 7
MAD_SCALE = 0.6745

# Smallest absolute change worth flagging, in the metric's registered unit.
MIN_CHANGE = {
    "body_weight": 1.5,  # kg
    "resting_heart_rate": 8.0,  # bpm
    "sleep_duration": 2.0,  # h
    "step_count": 4000.0,  # count
}


class Direction(StrEnum):
    UP = "up"
    DOWN = "down"


@dataclass(frozen=True)
class Anomaly:
    metric: str
    value: float
    baseline: float
    score: float  # absolute robust z-score
    direction: Direction


def detect(metric: str, series: Sequence[float]) -> Anomaly | None:
    """``series`` is oldest to newest; the last value is the one under test."""
    if metric not in MIN_CHANGE:
        return None  # unregistered metrics are not guessed at
    if any(not math.isfinite(v) for v in series):
        raise ValueError("series contains a non-finite value")
    if len(series) < MIN_BASELINE_POINTS + 1:
        return None  # not enough history to know what normal is
    *history, latest = series
    median = statistics.median(history)
    mad = statistics.median(abs(v - median) for v in history)
    change = latest - median
    if abs(change) < MIN_CHANGE[metric]:
        return None
    # a perfectly flat baseline with a real change is infinitely far from "normal"
    score = math.inf if mad == 0 else abs(MAD_SCALE * change / mad)
    if score < Z_LIMIT:
        return None
    return Anomaly(metric, latest, median, score, Direction.UP if change > 0 else Direction.DOWN)
