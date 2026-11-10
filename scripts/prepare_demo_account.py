"""Prepare the accounts for recording the video (local only, made-up data) (D105).

    uv run python scripts/prepare_demo_account.py [--sleep rested|debt] [--base http://127.0.0.1:8000]

Needs the dev stack and the API running (with ``VITALMESH_REQUIRE_VERIFIED=0``). It signs up
``prof.demo@example.org`` through the API, fills the profile and two weeks of made-up sleep,
steps, activity minutes, weight and one flagged lab, and creates an operator account
(``ops.demo@example.org``) for the /ops console. The passwords are generated and written to
``evals/results/demo-accounts.txt`` (git-ignored); they are never printed.

``--sleep rested`` (default) gives about eight hours a night, so a workout question gets a strength
day; ``--sleep debt`` gives about 6.8 hours, so it correctly gets an easy-cardio day.
If the account already exists, delete it first (the app's delete button), then run this again.
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "evals" / "results" / "demo-accounts.txt"
USER_EMAIL = "prof.demo@example.org"
OPS_EMAIL = "ops.demo@example.org"
PROFILE = {
    "sex": "female", "age_years": 34, "weight_kg": 68, "height_cm": 168, "activity": "moderate",
    "resting_hr_bpm": 62, "goal": "maintain", "diet": "vegan", "allergens": ["egg"],
    "conditions": [], "medications": [], "consent": True,
}  # fmt: skip
SLEEP = {
    "rested": [8.1, 7.6, 8.3, 7.9, 8.4, 7.7, 8.2, 8.0, 7.8, 8.5, 7.9, 8.1, 8.3, 7.7],
    "debt": [7.2, 6.4, 7.5, 5.9, 7.1, 6.8, 7.4, 6.2, 7.0, 7.3, 6.9, 6.6, 7.1, 6.7],
}
STEPS = [6200, 8100, 9800, 5400, 10300, 7600, 8800, 4900, 9100, 8300, 7700, 9400, 6800, 8600]
ACTIVE = [35, 40, 55, 45, 60, 35, 45, 40, 50, 40, 45, 55, 38, 42]
WEIGHT = [68.4, 68.2, 68.3, 68.0, 67.9, 68.1, 67.8, 67.9, 67.7, 67.8, 67.9, 67.7, 67.8, 67.6]


def remove_old_accounts() -> None:
    """Delete the two demo accounts from the local Postgres (made-up people only). Their Redis
    data goes when Redis is restarted (it has no persistence) or with ``purge_user``."""
    import psycopg

    dsn = os.environ.get("VITALMESH_PG_DSN", "postgresql://vitalmesh@127.0.0.1:5432/vitalmesh")
    if urlparse(dsn).hostname not in ("127.0.0.1", "localhost"):
        sys.exit("refusing: local Postgres only")
    with psycopg.connect(dsn, autocommit=True) as conn:
        gone = conn.execute(
            "delete from users where email in (%s, %s) returning email", (USER_EMAIL, OPS_EMAIL)
        ).fetchall()
    print(f"removed {len(gone)} old demo account(s)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--sleep", choices=sorted(SLEEP), default="rested")
    ap.add_argument("--replace", action="store_true", help="delete the two demo accounts first")
    args = ap.parse_args()
    if urlparse(args.base).hostname not in ("127.0.0.1", "localhost", "::1"):
        sys.exit("refusing: loopback only")

    if args.replace:
        remove_old_accounts()
    password = secrets.token_urlsafe(12) + "Aa1!"
    ops_password = secrets.token_urlsafe(12) + "Aa1!"
    c = httpx.Client(base_url=args.base, timeout=60)
    r = c.post("/v1/auth/signup", json={"email": USER_EMAIL, "password": password})
    if r.status_code != 201:
        sys.exit(f"signup failed ({r.status_code}): delete {USER_EMAIL} first if it exists")
    uid = r.json()["user_id"]

    def csrf() -> dict[str, str]:
        return {"x-csrf-token": c.cookies.get("vm_csrf") or ""}

    version = c.get("/v1/auth/choices").json()["consent_version"]
    r = c.put("/v1/me/profile", json={**PROFILE, "consent_version": version}, headers=csrf())
    if r.status_code != 200:
        sys.exit(f"profile failed ({r.status_code})")
    now = datetime.now(UTC)
    sleep = SLEEP[args.sleep]
    n = len(sleep)
    failures = 0
    for i in range(n):
        when = (now - timedelta(days=n - i)).replace(hour=7, minute=30).isoformat()
        for metric, value, unit in (
            ("sleep_duration", sleep[i], "h"),
            ("step_count", STEPS[i], "count"),
            ("active_minutes", ACTIVE[i], "min"),
            ("body_weight", WEIGHT[i], "kg"),
        ):
            r = c.post(
                "/v1/measurements",
                headers=csrf(),
                json={"user_id": uid, "metric_type": metric, "value": value, "unit": unit,
                      "measured_at": when},
            )  # fmt: skip
            failures += r.status_code != 202
    lab = {
        "user_id": uid, "marker_name": "LDL Cholesterol", "value": 128, "unit": "mg/dL",
        "reference_range": "< 100", "collected_at": (now - timedelta(days=12)).isoformat(),
    }  # fmt: skip
    failures += c.post("/v1/labs", headers=csrf(), json=lab).status_code != 202
    time.sleep(5)  # the processor writes the history behind the 202s
    export = c.get("/v1/me/export")
    series = export.json().get("data", {}).get("history", {}) if export.status_code == 200 else {}
    held = len(series)  # how many measurement series the processor has written (expect 4)

    env = {**os.environ, "VITALMESH_NEW_PASSWORD": ops_password}
    create = str(ROOT / "scripts" / "create_account.py")
    made = subprocess.run(
        [sys.executable, create, OPS_EMAIL, "--role", "operator"],
        env=env, capture_output=True, text=True, cwd=ROOT,
    )  # fmt: skip
    ops_ok = made.returncode == 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        f"video demo accounts (made-up data, local only)\n"
        f"person   {USER_EMAIL}  {password}   sleep={args.sleep}\n"
        f"operator {OPS_EMAIL}  {ops_password}   (for /ops){'' if ops_ok else '  NOT CREATED'}\n",
        encoding="utf-8",
    )
    print(
        f"person {USER_EMAIL}: {n} nights of sleep ({args.sleep}), posting failures: {failures},"
        f" measurement series held: {held} of 4"
    )
    print(f"operator {OPS_EMAIL}: {'created' if ops_ok else 'NOT created: ' + made.stderr[-200:]}")
    print(f"passwords written to {OUT.relative_to(ROOT)} (git-ignored; not printed)")
    if failures or not ops_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
