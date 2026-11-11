"""Create the reviewer's test accounts on a deployed demo (D108, 2026-11-12).

    uv run python scripts/seed_reviewer_accounts.py --base https://<demo-address>
        [--accounts-file evals/results/professor-accounts.txt]

Reads the accounts from a git-ignored file (one per line: ``kind email password``), signs each one
up through the public API (the demo has email confirmation off), and gives the three demo people
made-up data for the last 14 days, so a question gets a real, personal answer at once:

* ``steady``      34, healthy BMI, goal maintain, vegan with an egg allergy, rested sleep: every
                  area on track.
* ``weightloss``  42, BMI about 27.8, goal lose, weight falling 0.1 kg a day, sleep a little short,
                  one flagged lab: a gradual weight milestone, areas that need attention.
* ``gain``        29, BMI about 20.2, goal gain, vegetarian with a nut allergy, short sleep, little
                  activity: no weight target for a gain goal, easy-cardio workouts.
* ``empty``       signed up and nothing else: no profile, no data, so the reviewer sees the
                  first-time experience (the three-step onboarding).

The demo sits behind a shared site password (user ``reviewer``): give it with ``--gate-password``
or the environment variable ``VITALMESH_GATE_PASSWORD``. It is never printed. Only a loopback
address or an https ``*.cloudapp.azure.com`` demo is accepted. All data is made up.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = ROOT / "evals" / "results" / "professor-accounts.txt"
DAYS = 14


@dataclass(frozen=True)
class Person:
    profile: dict[str, object]
    sleep: list[float]
    steps: list[int]
    active: list[int]
    weight: list[float]
    lab: tuple[str, float, str, str] | None = None  # name, value, unit, reference range


STEADY = Person(
    profile={
        "sex": "female", "age_years": 34, "weight_kg": 67.6, "height_cm": 168,
        "activity": "moderate", "resting_hr_bpm": 62, "goal": "maintain", "diet": "vegan",
        "allergens": ["egg"], "conditions": [], "medications": [], "consent": True,
    },
    sleep=[8.1, 7.6, 8.3, 7.9, 8.4, 7.7, 8.2, 8.0, 7.8, 8.5, 7.9, 8.1, 8.3, 7.7],
    steps=[6200, 8100, 9800, 5400, 10300, 7600, 8800, 4900, 9100, 8300, 7700, 9400, 6800, 8600],
    active=[35, 40, 55, 45, 60, 35, 45, 40, 50, 40, 45, 55, 38, 42],
    weight=[68.4, 68.2, 68.3, 68.0, 67.9, 68.1, 67.8, 67.9, 67.7, 67.8, 67.9, 67.7, 67.8, 67.6],
)  # fmt: skip
WEIGHTLOSS = Person(
    profile={
        "sex": "male", "age_years": 42, "weight_kg": 88.1, "height_cm": 178,
        "activity": "light", "resting_hr_bpm": 68, "goal": "lose", "diet": "omnivore",
        "allergens": [], "conditions": [], "medications": [], "consent": True,
    },
    sleep=[6.8, 6.4, 7.1, 6.6, 6.2, 7.0, 6.5, 6.9, 6.3, 6.7, 7.2, 6.4, 6.8, 6.6],
    steps=[5200, 6100, 7400, 4800, 6900, 8200, 5100, 6300, 7000, 5600, 6800, 7900, 5300, 6400],
    active=[25, 30, 40, 20, 35, 45, 25, 30, 38, 28, 33, 42, 27, 32],
    weight=[89.5 - 0.1 * i for i in range(DAYS)],
    lab=("LDL Cholesterol", 128, "mg/dL", "< 100"),
)  # fmt: skip
GAIN = Person(
    profile={
        "sex": "female", "age_years": 29, "weight_kg": 55.0, "height_cm": 165,
        "activity": "light", "resting_hr_bpm": 64, "goal": "gain", "diet": "vegetarian",
        "allergens": ["nuts"], "conditions": [], "medications": [], "consent": True,
    },
    sleep=[6.2, 5.9, 6.6, 6.0, 6.4, 5.8, 6.5, 6.1, 6.3, 5.9, 6.6, 6.2, 6.0, 6.4],
    steps=[4200, 3900, 5100, 4600, 3800, 5400, 4100, 4700, 3600, 5000, 4400, 4900, 3700, 4500],
    active=[10, 15, 20, 5, 12, 25, 10, 15, 8, 18, 12, 20, 10, 14],
    weight=[54.6 + 0.03 * i for i in range(DAYS)],
)  # fmt: skip
PEOPLE = {"steady": STEADY, "weightloss": WEIGHTLOSS, "gain": GAIN}
KINDS = (*PEOPLE, "empty")


def read_accounts(path: Path) -> list[tuple[str, str, str]]:
    """(kind, email, password) per line; blank lines and # comments are ignored."""
    out: list[tuple[str, str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind, email, password = line.split(None, 2)
        if kind not in KINDS:
            raise SystemExit(f"unknown account kind {kind!r}; expected one of {', '.join(KINDS)}")
        out.append((kind, email, password.strip()))
    return out


def seed_person(c: httpx.Client, email: str, password: str, person: Person, now: datetime) -> str:
    """Sign up, fill the profile and the series, return a one-line result (no secrets)."""
    r = c.post("/v1/auth/signup", json={"email": email, "password": password})
    if r.status_code == 409:
        return f"{email}: already exists, left as it is"
    if r.status_code != 201:
        return f"{email}: SIGNUP FAILED ({r.status_code})"
    uid = r.json()["user_id"]

    def csrf() -> dict[str, str]:
        return {"x-csrf-token": c.cookies.get("vm_csrf") or ""}

    version = c.get("/v1/auth/choices").json()["consent_version"]
    r = c.put("/v1/me/profile", json={**person.profile, "consent_version": version}, headers=csrf())
    if r.status_code != 200:
        return f"{email}: PROFILE FAILED ({r.status_code})"
    failures = 0
    for i in range(DAYS):
        when = (now - timedelta(days=DAYS - i)).replace(hour=7, minute=30).isoformat()
        for metric, value, unit in (
            ("sleep_duration", person.sleep[i], "h"),
            ("step_count", person.steps[i], "count"),
            ("active_minutes", person.active[i], "min"),
            ("body_weight", round(person.weight[i], 1), "kg"),
        ):
            r = c.post(
                "/v1/measurements",
                headers=csrf(),
                json={"user_id": uid, "metric_type": metric, "value": value, "unit": unit,
                      "measured_at": when},
            )  # fmt: skip
            failures += r.status_code != 202
    if person.lab:
        name, value, unit, ref = person.lab
        collected = (now - timedelta(days=12)).isoformat()
        lab = {"user_id": uid, "marker_name": name, "value": value, "unit": unit,
               "reference_range": ref, "collected_at": collected}  # fmt: skip
        failures += c.post("/v1/labs", headers=csrf(), json=lab).status_code != 202
    return f"{email}: signed up, profile saved, {DAYS} days of data posted, failures {failures}"


def seed_empty(c: httpx.Client, email: str, password: str) -> str:
    r = c.post("/v1/auth/signup", json={"email": email, "password": password})
    if r.status_code == 409:
        return f"{email}: already exists, left as it is"
    return (
        f"{email}: signed up with nothing else"
        if r.status_code == 201
        else (f"{email}: SIGNUP FAILED ({r.status_code})")
    )


def seed(
    base: str,
    accounts: list[tuple[str, str, str]],
    gate: str | None,
    now: datetime,
    transport: httpx.BaseTransport | None = None,
) -> list[str]:
    """Create every account. Each person gets their own client (their own session cookies)."""
    lines: list[str] = []
    for kind, email, password in accounts:
        auth = ("reviewer", gate) if gate else None
        with httpx.Client(base_url=base, timeout=60, auth=auth, transport=transport) as c:
            lines.append(
                seed_empty(c, email, password) if kind == "empty"
                else seed_person(c, email, password, PEOPLE[kind], now)
            )  # fmt: skip
    return lines


def allowed_base(base: str) -> bool:
    u = urlparse(base)
    if u.hostname in ("127.0.0.1", "localhost", "::1"):
        return True
    return u.scheme == "https" and bool(u.hostname) and u.hostname.endswith(".cloudapp.azure.com")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--base", required=True)
    ap.add_argument("--accounts-file", type=Path, default=DEFAULT_FILE)
    ap.add_argument("--gate-password", default=os.environ.get("VITALMESH_GATE_PASSWORD"))
    args = ap.parse_args()
    if not allowed_base(args.base):
        sys.exit("refusing: only a loopback address or an https *.cloudapp.azure.com demo")
    accounts = read_accounts(args.accounts_file)
    lines = seed(args.base, accounts, args.gate_password, datetime.now(UTC))
    time.sleep(6)  # the processor writes the history behind the 202s
    print("\n".join(lines))
    if any(
        "FAILED" in ln
        or (
            "failures 0" not in ln
            and "signed up with nothing" not in ln
            and "already exists" not in ln
        )
        for ln in lines
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
