"""Access-control red team against the RUNNING API (Phase 7, D95). Loopback only, throwaway users.

    uv run python scripts/redteam_access.py [--base http://127.0.0.1:8000] [--json out.json]

Plays an attacker with a few ordinary accounts: another person's data, forged and swapped tokens,
CSRF, role escalation, path tricks, brute force, enumeration and oversized input. Every check
states what must happen; a check that does not hold is printed as FINDING and the exit code is 1.
All accounts it makes are deleted at the end and the rate-limit counters it raised are cleared
(otherwise the operator could not sign up for an hour).

It refuses any base URL that is not loopback. Needs the dev stack and the API (accounts on).
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt
import redis

PROFILE: dict[str, Any] = {
    "sex": "female", "age_years": 34, "weight_kg": 68, "height_cm": 168, "activity": "moderate",
    "resting_hr_bpm": 62, "goal": "maintain", "diet": "vegan", "allergens": ["egg"],
    "conditions": [], "medications": [], "consent": True, "consent_version": "",
}  # fmt: skip
JSON = {"Content-Type": "application/json"}


@dataclass
class Account:
    email: str
    password: str
    user_id: str
    http: httpx.Client

    def csrf(self) -> dict[str, str]:
        return {"x-csrf-token": self.http.cookies.get("vm_csrf") or ""}

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.http.cookies.items())


@dataclass
class Ctx:
    base: str
    redis: redis.Redis
    rows: list[dict[str, object]] = field(default_factory=list)
    made: list[Account] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append({"check": name, "ok": ok, "detail": detail})
        print(f"  {'ok     ' if ok else 'FINDING'}  {name}" + (f"   [{detail}]" if detail else ""))

    def signup(self, tag: str, extra: dict[str, Any] | None = None) -> Account:
        c = httpx.Client(base_url=self.base, timeout=60)
        email = f"rt-{tag}-{secrets.token_hex(3)}@example.org"
        pw = secrets.token_urlsafe(18) + "Aa1!"
        r = c.post("/v1/auth/signup", json={"email": email, "password": pw, **(extra or {})})
        assert r.status_code == 201, r.text
        acct = Account(email, pw, r.json()["user_id"], c)
        self.made.append(acct)
        return acct

    def onboard(self, acct: Account) -> None:
        version = acct.http.get("/v1/auth/choices").json()["consent_version"]
        r = acct.http.put(
            "/v1/me/profile", json={**PROFILE, "consent_version": version}, headers=acct.csrf()
        )
        assert r.status_code == 200, r.text

    def raw(
        self, method: str, path: str, cookies: str = "", headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> tuple[int, bytes]:  # fmt: skip
        """Send the path EXACTLY as given (httpx would normalise dot segments)."""
        u = urlparse(self.base)
        conn = http.client.HTTPConnection(u.hostname or "127.0.0.1", u.port or 80, timeout=60)
        h = dict(headers or {})
        if cookies:
            h["Cookie"] = cookies
        conn.request(method, path, body=body or None, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data


def b64(obj: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def clear_limits(r: redis.Redis) -> None:
    for k in r.scan_iter(match="rl:*", count=1000):
        r.delete(k)


# ---------------------------------------------------------------------------- 1. anonymous
def anonymous(x: Ctx, b: Account) -> None:
    print("\n1. Not signed in")
    anon = httpx.Client(base_url=x.base, timeout=30)
    protected = [
        ("GET", "/v1/me"), ("GET", "/v1/me/export"), ("GET", "/v1/me/profile"),
        ("GET", f"/v1/users/{b.user_id}/history"), ("GET", f"/v1/users/{b.user_id}/labs"),
        ("GET", "/v1/ops/lag"), ("GET", "/v1/ops/runs"), ("GET", "/v1/clinician/reviews"),
        ("POST", "/v1/measurements"), ("POST", "/v1/labs"), ("DELETE", "/v1/me"),
        ("PUT", "/v1/me/profile"), ("GET", "/v1/me/nudges"), ("GET", "/v1/me/integrations"),
    ]  # fmt: skip
    codes = {f"{m} {p}": anon.request(m, p, json={}).status_code for m, p in protected}
    bad = [f"{k}={v}" for k, v in codes.items() if v != 401]
    x.check("every protected route answers 401 with no session", not bad, "; ".join(bad))
    pub = [anon.get("/healthz").status_code, anon.get("/v1/auth/choices").status_code]
    x.check("only health and the sign-up choices are public", pub == [200, 200], str(pub))


# --------------------------------------------------------------------- 2. another's data
def other_people(x: Ctx, a: Account, b: Account, now: str) -> None:
    print("\n2. Another person's data (A attacks B)")
    leaked = []
    for p in ("history", "labs", "mealplan", "recommendation", "series/body_weight"):
        s = a.http.get(f"/v1/users/{b.user_id}/{p}").status_code
        if s != 404:
            leaked.append(f"{p}={s}")
    x.check("reading B's history/labs/plan/recommendation/series is 404", not leaked, str(leaked))
    q = {"question": "what is my plan?"}
    s = a.http.post(f"/v1/users/{b.user_id}/questions", headers=a.csrf(), json=q).status_code
    x.check("asking a question AS B is refused", s == 404, str(s))
    s = a.http.get(f"/v1/users/{b.user_id}/runs/anything").status_code
    x.check("B's run view is refused", s == 404, str(s))
    lab = {"user_id": b.user_id, "marker_name": "LDL Cholesterol", "value": 1, "unit": "mg/dL",
           "reference_range": "< 100", "collected_at": now}  # fmt: skip
    s = a.http.post("/v1/labs", headers=a.csrf(), json=lab).status_code
    x.check("writing a lab INTO B's record (body user_id = B) is 403", s == 403, str(s))
    meas = {"user_id": b.user_id, "metric_type": "body_weight", "value": 40, "unit": "kg",
            "measured_at": now}  # fmt: skip
    s = a.http.post("/v1/measurements", headers=a.csrf(), json=meas).status_code
    x.check("writing a measurement INTO B's record is 403", s == 403, str(s))
    exp = a.http.get("/v1/me/export")
    x.check("A's export contains nothing of B", b.user_id not in exp.text, str(exp.status_code))
    labs = b.http.get(f"/v1/users/{b.user_id}/labs").json()
    x.check("B's lab record is unchanged after the attempts",
            len(labs) == 1 and labs[0].get("value") == 128, f"{len(labs)} lab(s)")  # fmt: skip


# --------------------------------------------------------------------------- 3. paths
def path_tricks(x: Ctx, a: Account, b: Account) -> None:
    print("\n3. Path tricks (must never return B's data to A)")
    ck, bu, au = a.cookie_header(), b.user_id, a.user_id
    paths = [
        f"/v1/users/{bu}/labs/", f"//v1/users/{bu}/labs",
        f"/v1/users/{au}/../{bu}/labs", f"/v1/users/{au}/%2e%2e/{bu}/labs",
        f"/v1/users/{au}%2F..%2F{bu}/labs", f"/V1/USERS/{bu}/LABS",
        f"/v1/users/{bu}%00/labs", f"/v1/users/{bu}/labs?user_id={au}",
        f"/v1/users/{bu[:-1]}%{ord(bu[-1]):02x}/labs", f"/v1/users/{bu};x=1/labs",
        f"/v1/users/{au},{bu}/labs", "/v1/users/*/labs", "/v1/users/%2A/labs",
    ]  # fmt: skip
    bad = []
    for p in paths:
        s, body = x.raw("GET", p, ck)
        if (s == 200 and b"LDL" in body) or s >= 500:
            bad.append(f"{p} -> {s}")
    x.check(f"{len(paths)} path/encoding tricks neither expose B's lab nor crash", not bad,
            "; ".join(bad))  # fmt: skip
    hdr = {**JSON, "X-HTTP-Method-Override": "DELETE", **a.csrf()}
    s, _ = x.raw("POST", "/v1/measurements", ck, hdr, b"{}")
    x.check("method-override header does nothing special", s in (400, 405, 422), str(s))


# --------------------------------------------------------------------------- 4. tokens
def tokens(x: Ctx, a: Account, b: Account) -> None:
    print("\n4. Tokens")
    tok = a.http.cookies.get("vm_access") or ""
    refresh_tok = a.http.cookies.get("vm_refresh") or ""
    claims = jwt.decode(tok, options={"verify_signature": False})
    att = httpx.Client(base_url=x.base, timeout=30)

    def bearer(t: str) -> int:
        return att.get("/v1/me", headers={"Authorization": f"Bearer {t}".strip()}).status_code

    x.check("a valid bearer works (control: the checks below do not fail for another reason)",
            bearer(tok) == 200)  # fmt: skip
    mine = [k for k, v in claims.items() if v == a.user_id]
    forged = jwt.encode({**claims, **dict.fromkeys(mine, b.user_id)}, "x" * 40, "HS256")
    x.check("token signed with a different secret is refused", bearer(forged) == 401)
    head = b64({"alg": "none", "typ": "JWT"})
    x.check('"alg: none" token is refused', bearer(f"{head}.{b64(claims)}.") == 401)
    h, _, sig = tok.split(".")
    swapped = {**claims, **dict.fromkeys(mine, b.user_id)}
    x.check("payload edited to B's id, A's old signature: refused",
            bearer(f"{h}.{b64(swapped)}.{sig}") == 401)  # fmt: skip
    x.check("payload edited to role=operator, A's old signature: refused",
            bearer(f"{h}.{b64({**claims, 'role': 'operator'})}.{sig}") == 401)  # fmt: skip
    junk = ("", "abc", "a.b.c", tok[:-5], "Bearer " + tok)
    x.check("garbage / empty / truncated bearer values are refused",
            all(bearer(t) == 401 for t in junk))  # fmt: skip
    x.check("the refresh token is not accepted as an access token", bearer(refresh_tok) == 401)
    s = att.post("/v1/auth/refresh", headers={"Authorization": f"Bearer {tok}"}).status_code
    x.check("an access token cannot be used to refresh", s == 401, str(s))
    # refresh-token reuse: use the cookie once, then the OLD value again
    c2 = httpx.Client(base_url=x.base, timeout=30)
    c2.cookies.set("vm_refresh", refresh_tok)
    s1 = c2.post("/v1/auth/refresh").status_code
    c3 = httpx.Client(base_url=x.base, timeout=30)
    c3.cookies.set("vm_refresh", refresh_tok)
    s2 = c3.post("/v1/auth/refresh").status_code
    x.check("a refresh token works once; REUSE of the old one is refused",
            (s1, s2) == (200, 401), f"{s1},{s2}")  # fmt: skip
    s3 = c2.post("/v1/auth/refresh").status_code
    x.check("after reuse is detected the whole session is revoked (the new token too)",
            s3 == 401, str(s3))  # fmt: skip


# ---------------------------------------------------------------------------- 5. CSRF
def csrf_and_headers(x: Ctx, a: Account) -> None:
    print("\n5. CSRF, cross-origin and headers")
    s = a.http.post("/v1/me/verify/resend").status_code
    x.check("cookie session without the CSRF header is refused", s == 403, str(s))
    s = a.http.post("/v1/me/verify/resend", headers={"x-csrf-token": "wrong"}).status_code
    x.check("wrong CSRF header is refused", s == 403, str(s))
    s = a.http.put("/v1/me/profile", json=PROFILE, headers={"x-csrf-token": ""}).status_code
    x.check("empty CSRF header is refused", s == 403, str(s))
    evil = {"Origin": "https://evil.example", "Access-Control-Request-Method": "PUT"}
    pre = httpx.options(f"{x.base}/v1/me/profile", headers=evil)
    x.check("a cross-origin preflight is not granted",
            "access-control-allow-origin" not in {k.lower() for k in pre.headers},
            str(pre.status_code))  # fmt: skip
    g = a.http.get("/v1/me", headers={"Origin": "https://evil.example"})
    names = {k.lower() for k in g.headers}
    x.check("responses carry no CORS allowance for a foreign origin",
            "access-control-allow-origin" not in names)  # fmt: skip
    need = ["x-content-type-options", "content-security-policy", "referrer-policy",
            "cross-origin-opener-policy"]  # fmt: skip
    x.check("security headers are present", all(n in names for n in need),
            ",".join(n for n in need if n not in names))  # fmt: skip
    lg = httpx.post(f"{x.base}/v1/auth/login", json={"email": a.email, "password": a.password})
    ck = {c.split("=", 1)[0]: c.lower() for c in lg.headers.get_list("set-cookie")}
    ok = (
        "httponly" in ck.get("vm_access", "")
        and "httponly" in ck.get("vm_refresh", "")
        and all("samesite" in ck.get(n, "") for n in ("vm_access", "vm_refresh", "vm_csrf"))
    )
    x.check("login cookies: access and refresh HttpOnly, all three SameSite", ok, ",".join(ck))


# ---------------------------------------------------------------------------- 6. roles
def roles(x: Ctx, a: Account, b: Account) -> None:
    print("\n6. Roles")
    paths = ["lag", "runs", "graph", "evals", "nudges", "tiers"]
    codes = [a.http.get(f"/v1/ops/{p}").status_code for p in paths]
    codes.append(a.http.get("/v1/clinician/reviews").status_code)
    x.check("a normal user gets 403 on every /v1/ops and /v1/clinician route",
            all(c == 403 for c in codes), str(codes))  # fmt: skip
    dec = {"decision": "approve"}
    s = a.http.post("/v1/clinician/reviews/x/decision", headers=a.csrf(), json=dec).status_code
    x.check("a normal user cannot decide a clinician review", s == 403, str(s))
    s = a.http.get("/ops").status_code
    page = a.http.get("/ops").text.lower()
    x.check("the /ops page itself carries no data (it is a shell)", s == 200 and "ldl" not in page)
    email = f"rt-role-{secrets.token_hex(3)}@example.org"
    pw = secrets.token_urlsafe(18) + "Aa1!"
    c = httpx.Client(base_url=x.base, timeout=30)
    r = c.post("/v1/auth/signup", json={"email": email, "password": pw, "role": "operator"})
    if r.status_code == 201:
        x.made.append(Account(email, pw, r.json()["user_id"], c))
        who = c.get("/v1/me").json()
        x.check("a 'role' field in the sign-up body does not make an operator",
                who.get("role") == "user", str(who.get("role")))  # fmt: skip
    else:
        x.check("a 'role' field in the sign-up body is refused", r.status_code == 422,
                str(r.status_code))  # fmt: skip
    body = {**PROFILE, "role": "operator", "user_id": b.user_id}
    s = a.http.put("/v1/me/profile", headers=a.csrf(), json=body).status_code
    x.check("profile update with extra role/user_id fields is refused", s == 422, str(s))


# ----------------------------------------------------------------------- 7. hardening
def hardening(x: Ctx, a: Account, now: str) -> None:
    print("\n7. Input hardening")
    ck = a.cookie_header()
    hdr = {**JSON, **a.csrf()}
    tmpl = '{"user_id":"%s","metric_type":"body_weight","value":%s,"unit":"kg","measured_at":"%s"}'
    for name, val in (("non-finite", "NaN"), ("absurd", "1e308"), ("negative", "-5")):
        body = (tmpl % (a.user_id, val, now)).encode()
        s, _ = x.raw("POST", "/v1/measurements", ck, hdr, body)
        x.check(f"{name} measurement is refused cleanly (4xx, not 5xx)", 400 <= s < 500, str(s))
    big = b'{"x":"' + b"A" * (6 * 1024 * 1024) + b'"}'
    t0 = time.perf_counter()
    s, _ = x.raw("POST", "/v1/measurements", ck, hdr, big)
    x.check("a 6 MB JSON body is refused without crashing", 400 <= s < 500,
            f"{s} in {time.perf_counter() - t0:.1f}s")  # fmt: skip
    t0 = time.perf_counter()
    s = httpx.post(f"{x.base}/v1/auth/login", timeout=60,
                   json={"email": a.email, "password": "A1!" * 400_000}).status_code  # fmt: skip
    took = time.perf_counter() - t0
    x.check("a 1.2 MB password is refused quickly (no hashing DoS)",
            400 <= s < 500 and took < 5, f"{s} in {took:.1f}s")  # fmt: skip
    odd = ["a'--@x.org", "<script>alert(1)</script>@x.org", "x" * 400 + "@x.org", "no-at-sign"]
    codes = [
        httpx.post(
            f"{x.base}/v1/auth/login", json={"email": e, "password": "whatever1A!x"}
        ).status_code
        for e in odd
    ]
    x.check("odd emails (quote, script tag, 400 chars, no @) never cause a 5xx",
            all(c < 500 for c in codes), str(codes))  # fmt: skip
    s = a.http.post(f"/v1/users/{a.user_id}/questions", headers=a.csrf(),
                    json={"question": "x" * 200_000}).status_code  # fmt: skip
    x.check("a 200,000-character question is refused (4xx)", 400 <= s < 500, str(s))


# --------------------------------------------------------------------- 8. brute force
def brute_force(x: Ctx, a: Account) -> None:
    print("\n8. Enumeration and brute force")
    ghost = f"rt-ghost-{secrets.token_hex(3)}@example.org"
    bad_pw = "Wrong1pass!x"
    un = httpx.post(f"{x.base}/v1/auth/login", json={"email": ghost, "password": bad_pw})
    wr = httpx.post(f"{x.base}/v1/auth/login", json={"email": a.email, "password": bad_pw})
    x.check("unknown email and wrong password give the same status and message",
            (un.status_code, un.json()) == (wr.status_code, wr.json()),
            f"{un.status_code} vs {wr.status_code}")  # fmt: skip
    known = httpx.post(f"{x.base}/v1/auth/reset/request", json={"email": a.email})
    unk = httpx.post(f"{x.base}/v1/auth/reset/request", json={"email": ghost})
    x.check("password-reset request answers identically for known and unknown emails",
            (known.status_code, known.json()) == (unk.status_code, unk.json()),
            f"{known.status_code} vs {unk.status_code}")  # fmt: skip
    pw = secrets.token_urlsafe(18) + "Aa1!"
    dup = httpx.post(f"{x.base}/v1/auth/signup", json={"email": a.email, "password": pw})
    print(
        f"  note     sign-up with an existing email answers {dup.status_code}: it reveals that an "
        "email is registered (a known, common trade-off, listed as a gap)"
    )
    victim = x.signup("victim")
    codes = [
        httpx.post(f"{x.base}/v1/auth/login",
                   json={"email": victim.email, "password": f"Wrong{i}pass!x"}).status_code
        for i in range(14)
    ]  # fmt: skip
    x.check("repeated wrong passwords end in 429 (rate limit / lockout)", 429 in codes,
            str(sorted(set(codes))))  # fmt: skip
    good = httpx.post(
        f"{x.base}/v1/auth/login", json={"email": victim.email, "password": victim.password}
    ).status_code
    x.check("while locked out even the CORRECT password is refused", good == 429, str(good))
    flood = [
        httpx.post(f"{x.base}/v1/auth/signup",
                   json={"email": f"rt-f{i}-{secrets.token_hex(2)}@example.org",
                         "password": "short"}).status_code
        for i in range(12)
    ]  # fmt: skip
    x.check("a sign-up flood from one address ends in 429", 429 in flood,
            str(sorted(set(flood))))  # fmt: skip
    resets = [httpx.post(f"{x.base}/v1/auth/reset/request",
                         json={"email": ghost}).status_code for _ in range(12)]  # fmt: skip
    x.check("a password-reset flood ends in 429", 429 in resets, str(sorted(set(resets))))


def cleanup(x: Ctx) -> None:
    print("\nCleanup")
    clear_limits(x.redis)
    deleted = 0
    for acct in x.made:
        try:
            c = acct.http
            d = c.request("DELETE", "/v1/me", json={"password": acct.password},
                          headers=acct.csrf())  # fmt: skip
            if d.status_code == 401:  # a token test revoked this session: sign in again
                c = httpx.Client(base_url=x.base, timeout=60)
                login = {"email": acct.email, "password": acct.password}
                if c.post("/v1/auth/login", json=login).status_code == 200:
                    csrf = {"x-csrf-token": c.cookies.get("vm_csrf") or ""}
                    d = c.request("DELETE", "/v1/me", json={"password": acct.password},
                                  headers=csrf)  # fmt: skip
            deleted += d.status_code == 200
            if d.status_code != 200:
                print(f"  could not delete {acct.email}: {d.status_code}")
        except Exception as exc:  # report, never hide
            print(f"  cleanup problem: {type(exc).__name__}")
    clear_limits(x.redis)
    print(f"  deleted {deleted} of {len(x.made)} throwaway accounts")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    if urlparse(args.base).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("refusing: this tool only attacks a loopback address")
    x = Ctx(args.base, redis.Redis.from_url("redis://127.0.0.1:6379/0"))
    try:
        a, b = x.signup("a"), x.signup("b")
        x.onboard(a)
        x.onboard(b)
        now = datetime.now(UTC).isoformat()
        lab = {"user_id": b.user_id, "marker_name": "LDL Cholesterol", "value": 128,
               "unit": "mg/dL", "reference_range": "< 100", "collected_at": now}  # fmt: skip
        assert b.http.post("/v1/labs", headers=b.csrf(), json=lab).status_code == 202
        for _ in range(60):  # the processor writes the lab; without it the checks are vacuous
            if b.http.get(f"/v1/users/{b.user_id}/labs").json():
                break
            time.sleep(0.5)
        else:
            x.check("precondition: B's lab reached the store (is the processor running?)", False)
            raise SystemExit(1)
        anonymous(x, b)
        other_people(x, a, b, now)
        path_tricks(x, a, b)
        csrf_and_headers(x, a)
        roles(x, a, b)
        hardening(x, a, now)
        tokens(x, a, b)  # last of the session tests: it revokes A's session
        brute_force(x, b)
    finally:
        cleanup(x)
    bad = [r for r in x.rows if not r["ok"]]
    print(f"\n{len(x.rows) - len(bad)} of {len(x.rows)} checks held; {len(bad)} findings")
    if args.json:
        Path(args.json).write_text(json.dumps(x.rows, indent=1), encoding="utf-8")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
