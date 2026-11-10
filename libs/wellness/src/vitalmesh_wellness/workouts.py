"""Deterministic beginner workout catalogue and planner (D101). No LLM.

"What workout should I do?" must get a concrete answer: today's session with named exercises, sets
and reps, or a number of minutes of easy cardio in the user's own heart-rate zone. The model never
invents a lift or a number; it may only phrase what this module produced.

Scope, stated on purpose: general wellness for a healthy adult beginner. Generic ranges only
(three sets of eight to twelve reps), no maximal lifts and no one-rep-max, no weights (the user
picks a load they can control). A user with an intensity-limiting condition, or with any
condition the knowledge graph does not know, gets easy cardio only and a pointer to their
doctor: an unknown condition is never assumed safe. The catalogue is written for this project
(not taken from a licensed source).

The planner is not trusted: the Guardian's ``workout_safe`` rule re-checks every entry against the
catalogue (known exercise, sets, reps) and the claims' bounds, independently of this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from vitalmesh_wellness.claims import UserConstraints, WorkoutEntry
from vitalmesh_wellness.knowledge import DEFAULT_INDEX

PLANNER_VERSION = "1"
WARMUP_MINUTES = 5
MIN_MINUTES = 10
MAX_MINUTES = 90
MIN_STRENGTH_MINUTES = 25  # three exercises of three sets plus the warm-up
MINUTES_PER_EXERCISE = 7
MIN_EXERCISES = 3
MAX_EXERCISES = 8
MAX_SETS_PER_EXERCISE = 5
MAX_TOTAL_SETS = 24
CARDIO_ZONE = 2  # easy: you can still hold a conversation
BEGINNER_MAX_ZONE = 3  # no workout of this product goes above zone 3


class Place(StrEnum):
    GYM = "gym"
    HOME = "home"


class WorkoutError(ValueError):
    """No safe workout can be made for these inputs."""


@dataclass(frozen=True)
class Exercise:
    id: str
    name: str
    places: frozenset[Place]
    sets: int
    reps_low: int
    reps_high: int
    unit: str  # "reps" or "seconds"
    cue: str  # no digits: every number a user sees comes from a claim


_GYM = frozenset({Place.GYM})
_ANY = frozenset({Place.GYM, Place.HOME})


def _ex(
    id_: str,
    name: str,
    places: frozenset[Place],
    cue: str,
    low: int = 8,
    high: int = 12,
    unit: str = "reps",
) -> Exercise:
    return Exercise(id_, name, places, 3, low, high, unit, cue)


CATALOGUE: tuple[Exercise, ...] = (
    # push (gym)
    _ex(
        "bench_press",
        "Bench press",
        _GYM,
        "Lower the bar to mid-chest, press up; ask for a spotter.",
    ),
    _ex(
        "db_shoulder_press",
        "Dumbbell shoulder press",
        _GYM,
        "Press the dumbbells overhead, ribs down.",
    ),
    _ex(
        "incline_db_press",
        "Incline dumbbell press",
        _GYM,
        "Press up and slightly back on a raised bench.",
    ),
    _ex(
        "triceps_pushdown",
        "Cable triceps pushdown",
        _GYM,
        "Elbows pinned to your sides, straighten the arms.",
    ),
    # pull (gym)
    _ex("lat_pulldown", "Lat pulldown", _GYM, "Pull the bar to your upper chest, shoulders down."),
    _ex(
        "seated_row",
        "Seated cable row",
        _GYM,
        "Pull the handle to your waist, squeeze the shoulder blades.",
    ),
    _ex(
        "db_row",
        "One-arm dumbbell row",
        _GYM,
        "Flat back, pull the dumbbell to your hip.",
        unit="reps",
    ),
    _ex(
        "face_pull",
        "Cable face pull",
        _GYM,
        "Pull the rope towards your face, elbows high.",
        12,
        15,
    ),
    _ex("biceps_curl", "Dumbbell biceps curl", _GYM, "Curl up without swinging your body."),
    # legs (gym)
    _ex(
        "leg_press",
        "Leg press",
        _GYM,
        "Feet shoulder-width, lower under control, do not lock the knees.",
    ),
    _ex(
        "goblet_squat",
        "Goblet squat",
        _GYM,
        "Hold one dumbbell at the chest, sit down between your hips.",
    ),
    _ex(
        "db_romanian_deadlift",
        "Dumbbell Romanian deadlift",
        _GYM,
        "Push the hips back, flat back, feel the hamstrings.",
    ),
    _ex("leg_curl", "Machine leg curl", _GYM, "Curl the pad under control, no jerking."),
    _ex(
        "calf_raise",
        "Standing calf raise",
        _GYM,
        "Rise onto the toes, pause, lower slowly.",
        12,
        15,
    ),
    # full body (home, or anywhere)
    _ex(
        "push_up",
        "Push-up",
        _ANY,
        "Hands under the shoulders, body in one line; knees down is fine.",
        6,
        12,
    ),
    _ex(
        "bodyweight_squat",
        "Bodyweight squat",
        _ANY,
        "Sit back and down, chest up, knees over the toes.",
        10,
        15,
    ),
    _ex(
        "glute_bridge",
        "Glute bridge",
        _ANY,
        "Lie on your back, push the hips up, squeeze at the top.",
        10,
        15,
    ),
    _ex(
        "backpack_row",
        "Backpack row",
        _ANY,
        "Bend forward with a loaded backpack, pull it to your waist.",
    ),
    _ex(
        "reverse_lunge",
        "Reverse lunge",
        _ANY,
        "Step back, lower the back knee, push through the front foot.",
        8,
        10,
    ),
    _ex(
        "dead_bug", "Dead bug", _ANY, "Lie on your back, lower opposite arm and leg slowly.", 8, 10
    ),
    _ex(
        "plank", "Plank", _ANY, "Forearms down, body straight, breathe normally.", 20, 40, "seconds"
    ),
)
BY_ID: dict[str, Exercise] = {e.id: e for e in CATALOGUE}

SESSIONS: dict[str, tuple[str, ...]] = {
    "push": ("bench_press", "db_shoulder_press", "incline_db_press", "triceps_pushdown", "plank"),
    "pull": ("lat_pulldown", "seated_row", "db_row", "face_pull", "biceps_curl"),
    "legs": ("leg_press", "goblet_squat", "db_romanian_deadlift", "leg_curl", "calf_raise"),
    "full_body": (
        "push_up",
        "bodyweight_squat",
        "glute_bridge",
        "backpack_row",
        "reverse_lunge",
        "dead_bug",
        "plank",
    ),
}
SESSION_LABEL = {
    "push": "push day (chest, shoulders, triceps)",
    "pull": "pull day (back and biceps)",
    "legs": "leg day",
    "full_body": "full-body session",
    "cardio": "easy cardio",
}
ROTATION: dict[Place, tuple[str, ...]] = {
    Place.GYM: ("push", "pull", "legs", "cardio"),
    Place.HOME: ("full_body", "cardio"),
}


@dataclass(frozen=True)
class WorkoutPlan:
    session: str  # a key of SESSIONS, or "cardio"
    place: Place
    minutes: int
    entries: tuple[WorkoutEntry, ...]  # empty for cardio
    cardio_zone: int | None  # set for cardio
    why: str  # "rotation", "requested", "recovery", "conditions", "short"


def strength_allowed(conditions: frozenset[str] | set[str]) -> bool:
    """Strength work needs every declared condition to be one the knowledge graph knows AND that
    does not limit intensity. An empty list is fine; an unknown condition is never assumed safe."""
    folded = {c.strip().lower() for c in conditions if c.strip()}
    neutral = DEFAULT_INDEX.known_conditions() - DEFAULT_INDEX.limiting_conditions()
    return folded <= neutral


def exercise_count(minutes: int, available: int) -> int:
    fit = (minutes - WARMUP_MINUTES) // MINUTES_PER_EXERCISE
    return max(MIN_EXERCISES, min(fit, available, MAX_EXERCISES))


def plan_workout(
    user: UserConstraints,
    day: date,
    place: Place,
    minutes: int,
    recovering: bool = False,
    focus: str | None = None,
) -> WorkoutPlan:
    if not MIN_MINUTES <= minutes <= MAX_MINUTES:
        raise WorkoutError(f"a workout is {MIN_MINUTES} to {MAX_MINUTES} minutes")
    rotation = ROTATION[place]
    if not strength_allowed(user.conditions):
        return WorkoutPlan("cardio", place, minutes, (), CARDIO_ZONE, "conditions")
    if recovering:
        return WorkoutPlan("cardio", place, minutes, (), CARDIO_ZONE, "recovery")
    wanted = (focus or "").strip().lower()
    if wanted in rotation:
        session, why = wanted, "requested"
    else:
        session, why = rotation[day.toordinal() % len(rotation)], "rotation"
    if session == "cardio":
        return WorkoutPlan("cardio", place, minutes, (), CARDIO_ZONE, why)
    if minutes < MIN_STRENGTH_MINUTES:
        return WorkoutPlan("cardio", place, minutes, (), CARDIO_ZONE, "short")
    ids = SESSIONS[session]
    chosen = ids[: exercise_count(minutes, len(ids))]
    entries = tuple(
        WorkoutEntry(
            day=day,
            exercise_id=i,
            sets=BY_ID[i].sets,
            reps_low=BY_ID[i].reps_low,
            reps_high=BY_ID[i].reps_high,
        )
        for i in chosen
    )
    return WorkoutPlan(session, place, minutes, entries, None, why)
