# VitalMesh threat model

Scope: the web application (accounts, onboarding, questions, runs), the Kafka agent graph behind it, and the stores (Postgres, Redis). Written 2026-10-24 against the code at that date. It lists what we defend, who we defend it from, what is built, the test that proves each defence, and what is **not** defended. Nothing here is a claim of certification or penetration testing: the red-team phase is still ahead.

## 1. Assets (what is worth protecting)

| Asset | Why it matters |
|---|---|
| Health profile, measurements, lab values | Sensitive personal data; the product's reason to be trusted |
| Credentials, refresh tokens, signing secret | Whoever has them is the person |
| The recommendation shown to a person | A wrong or unsafe answer can cause physical harm |
| Model quota (free tier) | A shared, small budget; exhausting it denies service to everyone |
| Audit trail | Evidence of who did what |

## 2. Actors

- **Anonymous visitor**: can reach sign-up and sign-in only.
- **Signed-in person**: may reach their own data only.
- **Malicious signed-in person**: tries to reach someone else's data, or to exhaust quota.
- **Operator / clinician**: sees operator routes (tier mix, run internals); never another person's profile through the user routes.
- **Attacker on the network or a malicious web page**: tries CSRF, token theft, or script injection.
- **The language model itself**: treated as untrusted. It can be wrong, or be steered by text in a question.

## 3. Trust boundaries

1. Browser to API (internet side).
2. API to Redis, Postgres, Kafka (internal; ports bound to 127.0.0.1 locally).
3. Agents to the model provider (data leaves the system; only synthetic or public data is ever sent).
4. Model output to the person (the code verifies; the Safety Guardian is the last step).

## 4. Threats, defences, and the test that proves them

| # | Threat | Defence in code | Evidence |
|---|---|---|---|
| T1 | Guessing or stuffing passwords | argon2id hashing; NIST 800-63B password policy (length, common-password and email checks); lockout after 5 failures in 15 minutes; per-address rate limiter | `libs/accounts` tests, `scripts/mutation_check_security.py` |
| T2 | Stolen access token | HS256 token with a 15-minute life; algorithm pinned on verify; secret from `VITALMESH_AUTH_SECRET` (a random per-process secret and a loud warning if unset) | `test_accounts.py` |
| T3 | Stolen refresh token | Single-use rotation; only the SHA-256 of a token is stored; presenting an already-used token revokes the whole session (reuse detection) | store contract tests (in-memory and Postgres) |
| T4 | Reading or changing another person's data (broken access control) | Deny-by-default gate middleware; every `/v1/users/{id}` and `/v1/me` route authorises "own data only" and answers 404 (not 403) for others; ops routes need the operator role | `test_security.py`; mutation check removes each rule and the tests fail |
| T5 | CSRF | Double-submit token cookie plus header on every state-changing request; cookies are HttpOnly (session) and SameSite | mutation check (CSRF rule removed, caught) |
| T6 | Script injection | Per-response nonce Content-Security-Policy; no inline script without the nonce; no CORS; `no-store` caching on private responses | header tests |
| T7 | Quota exhaustion by one person | 20 questions per hour per person; token-weighted load monitor sheds to a no-model answer (T0) when the free tier is near its limit | rate-limit and load-shed tests; live demo of shedding |
| T8 | Prompt injection in a question | Input screen (first layer only: it missed 8 of 8 held-out phrasings, D61); the model's output is checked in code (citations, numbers against tool results); the Safety Guardian runs at drafting and again at release; risky outputs go to human approval | evals, T3 graph tests |
| T9 | Unsafe or fabricated health advice | The model never produces numbers or compliance decisions; claims carry provenance; unknown is never safe; deterministic fallback text | Guardian parity tests, 136-case evals |
| T10 | Data kept after a person leaves | Export and erase reach Postgres and Redis; consent is versioned; erase is audited | `test_userdata.py` (integration) |
| T11 | Known-vulnerable dependencies | `pip-audit` on the locked set and `bandit` run in CI on every push; `uv.lock` is checked | CI job "dependency and code scan"; D77 |
| T12 | SQL injection | All values are bound parameters; the only string-built SQL uses constant column lists (annotated for the scanner) | bandit, review |
| T13 | Secrets in the repository | `.env` ignored; key-pattern scan and `git ls-files` check before every push; no real data from `private/` anywhere tracked | pre-push checks |
| T14 | Sensitive data in logs or traces | Tokens, cookies and passwords are never logged; traces contain synthetic or public text only | review; D65 |
| T15 | Account takeover through the email links, or enumeration of registered addresses | Reset and verify links are 32 random bytes, stored only as SHA-256, single use (atomic consume), expire (30 min and 24 h), tied to a purpose, and a newer link replaces the older; the token travels in the URL fragment so it never reaches a server log or Referer; the reset request answers identically for unknown addresses and sends mail in the background; weak passwords do not burn the link; a reset revokes every session | 7 of the 12 mutation-check rules, `test_security.py` |
| T16 | A hostile uploaded file (XML entity bomb, file-reading entity, zip bomb, huge file) or a poisoned value | `defusedxml` refuses entities and external references; upload 50 MB, decompressed 400 MB, 3,000,000 records and 200,000 CSV rows caps; every value passes the same range checks as manual entry (rejected, never clamped); nothing is stored until the owner confirms, and the preview is readable only by its owner for 15 minutes | 4 of the 16 mutation-check rules, `test_healthimport.py`, `test_imports_api.py` |

## 5. Known gaps (stated, not hidden)

- **Email verification and password reset exist, but there is no mail service.** The local build writes the links to the server log, so a real person cannot receive one; a deployment needs an SMTP or mail-API sender, and `LogMailer` (which puts a token in a log) must not run with real users. Timing of the reset answer is equalised by sending in the background, not measured.
- **No multi-factor authentication.**
- **Local-only transport.** Cookies are `Secure` only when `VITALMESH_SECURE_COOKIES=1`; real HTTPS and a secret store (Azure Key Vault) arrive with the cloud phase.
- **The input screen is weak** (a blocklist). The real defence is that the model cannot act: its output is verified and gated.
- **No penetration test or external review.** The mutation check proves our tests notice five removed rules; it does not prove there are no other holes.
- **Account layer not load-tested.** The 1M-user claim rests on the agent tiers; the account store at scale is still to be measured.
- **Browser behaviour is tested by hand once, not yet by an automated browser suite or an accessibility audit.**
- **Model provider sees question text.** Only synthetic or public data is ever sent; a real deployment needs a data-processing agreement and a decision on regional hosting.

## 6. How this is kept honest

Each row above names its test. A threat with no test goes in section 5. The red-team phase will attack this list and add what it finds.
