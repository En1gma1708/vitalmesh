"""How often does a real question get a real answer? (D100)  Loopback only, synthetic person.

    uv run python scripts/measure_answers.py [--label before] [--clear-llm-cache] [--json out.json]

Needs the dev stack, the processor, the agents (with the tracker role) and the API (accounts on,
``VITALMESH_REQUIRE_VERIFIED=0``). It signs up ONE throwaway person with made-up data, asks the
questions below one at a time through the same HTTP path the browser uses, waits for the run to end
and classifies the outcome:

  complete   released, every helper answered
  partial    released, but a helper is missing or failed (the answer says so)
  refused    the system refused ("I can't give this answer")
  held       waiting for a clinician
  no final   nothing final within the wait

It spends free model quota (about one full run per question), so it takes 10 to 40 minutes. The
account is deleted at the end. ``--clear-llm-cache`` first empties the model-answer cache of the
local Redis, so earlier runs cannot make the numbers look better.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
import redis

QUESTIONS = [
    "How many calories should I eat?",
    "How much should I sleep?",
    "What should I eat and how should I train this week?",
    "should i do cardio or gym?",
    "I want to build muscle, what should I do?",
    "Is it okay to exercise when I slept badly?",
    "What can I eat for breakfast on a vegan diet?",
    "How can I improve my sleep?",
    "How many steps a day should I aim for?",
    "What is a healthy resting heart rate?",
]
PROFILE = {
    "sex": "female", "age_years": 34, "weight_kg": 68, "height_cm": 168, "activity": "moderate",
    "resting_hr_bpm": 62, "goal": "maintain", "diet": "vegan", "allergens": ["egg"],
    "conditions": [], "medications": [], "consent": True,
}  # fmt: skip
SLEEP = [7.2, 6.4, 7.5, 5.9, 7.1, 6.8, 7.4, 6.2, 7.0, 7.3]


def classify(view: dict[str, object] | None) -> str:
    if view is None:
        return "no final"
    status = view.get("status")
    steps = " ".join(str(s.get("text", "")) for s in view.get("steps", []))  # type: ignore[attr-defined]
    answer = view.get("answer") or {}
    if status == "released":
        broken = "couldn't finish" in steps or "is missing" in steps or answer.get("partial_note")  # type: ignore[attr-defined]
        return "partial" if broken else "complete"
    if status == "blocked":
        return "refused"
    if status == "needs_review":
        return "held"
    return "no final"


def ask(c: httpx.Client, uid: str, question: str, wait_s: float) -> tuple[dict | None, float]:
    csrf = {"x-csrf-token": c.cookies.get("vm_csrf") or ""}
    t0 = time.perf_counter()
    q = c.post(f"/v1/users/{uid}/questions", headers=csrf, json={"question": question})
    if q.status_code != 202:
        return {"status": f"http {q.status_code}", "steps": []}, 0.0
    cid = q.json()["correlation_id"]
    final = None
    while time.perf_counter() - t0 < wait_s:
        r = c.get(f"/v1/users/{uid}/runs/{cid}")
        v = r.json() if r.status_code == 200 else {}
        if v.get("status") in ("released", "blocked", "needs_review"):
            final = v
            break
        time.sleep(2)
    return final if final is not None else {
        "status": "timeout",
        "steps": v.get("steps", []) if r.status_code == 200 else [],
    }, time.perf_counter() - t0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--label", default="run")
    ap.add_argument("--wait", type=float, default=300.0, help="seconds to wait per question")
    ap.add_argument("--only", type=int, default=0, help="only the first N questions")
    ap.add_argument("--clear-llm-cache", action="store_true")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    if urlparse(args.base).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("refusing: loopback only")
    if args.clear_llm_cache:
        r = redis.Redis.from_url("redis://127.0.0.1:6379/0")
        n = sum(1 for k in r.scan_iter(match="llm:cache:*", count=1000) if r.delete(k))
        print(f"cleared {n} cached model answers from the local Redis")
    questions = QUESTIONS[: args.only] if args.only else QUESTIONS

    c = httpx.Client(base_url=args.base, timeout=60)
    email, pw = f"measure-{secrets.token_hex(3)}@example.org", secrets.token_urlsafe(14) + "Aa1!"
    uid = c.post("/v1/auth/signup", json={"email": email, "password": pw}).json()["user_id"]
    rows: list[dict[str, object]] = []
    try:
        csrf = lambda: {"x-csrf-token": c.cookies.get("vm_csrf") or ""}  # noqa: E731
        version = c.get("/v1/auth/choices").json()["consent_version"]
        assert (
            c.put(
                "/v1/me/profile", json={**PROFILE, "consent_version": version}, headers=csrf()
            ).status_code
            == 200
        )
        now = datetime.now(UTC)
        for i, hours in enumerate(SLEEP):
            when = (now - timedelta(days=len(SLEEP) - i)).replace(hour=7, minute=30).isoformat()
            c.post(
                "/v1/measurements",
                headers=csrf(),
                json={
                    "user_id": uid,
                    "metric_type": "sleep_duration",
                    "value": hours,
                    "unit": "h",
                    "measured_at": when,
                },
            )
        time.sleep(4)
        print(f"{'#':>2} {'outcome':<9} {'seconds':>7}  question")
        for n, question in enumerate(questions, start=1):
            view, secs = ask(c, uid, question, args.wait)
            outcome = classify(view)
            answer = (view or {}).get("answer") or {}
            rows.append({
                "n": n, "question": question, "outcome": outcome, "seconds": round(secs, 1),
                "status": (view or {}).get("status"),
                "steps": [str(s.get("text", "")) for s in (view or {}).get("steps", [])],
                "answer_start": str(answer.get("text", ""))[:200],
                "partial_note": answer.get("partial_note"),
            })  # fmt: skip
            print(f"{n:>2} {outcome:<9} {secs:7.1f}  {question}", flush=True)
    finally:
        c.request(
            "DELETE",
            "/v1/me",
            json={"password": pw},
            headers={"x-csrf-token": c.cookies.get("vm_csrf") or ""},
        )
    counts: dict[str, int] = {}
    for r in rows:
        counts[str(r["outcome"])] = counts.get(str(r["outcome"]), 0) + 1
    print(
        f"\n{args.label}: "
        + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        + f" (of {len(rows)})"
    )
    out = {
        "label": args.label,
        "when": datetime.now(UTC).isoformat(),
        "counts": counts,
        "rows": rows,
    }
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1), encoding="utf-8")
    sys.exit(0)


if __name__ == "__main__":
    main()
