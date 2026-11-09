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
| T10 | Data kept after a person leaves | Export and erase reach Postgres and Redis, including the last 50 questions and the answers shown (kept in Postgres, readable by their owner only, D83); consent is versioned; erase is audited | `test_userdata.py` (integration) |
| T11 | Known-vulnerable dependencies | `pip-audit` on the locked set and `bandit` run in CI on every push; `uv.lock` is checked | CI job "dependency and code scan"; D77 |
| T12 | SQL injection | All values are bound parameters; the only string-built SQL uses constant column lists (annotated for the scanner) | bandit, review |
| T13 | Secrets in the repository | `.env` ignored; key-pattern scan and `git ls-files` check before every push; no real data from `private/` anywhere tracked | pre-push checks |
| T14 | Sensitive data in logs or traces | Tokens, cookies and passwords are never logged; traces contain synthetic or public text only | review; D65 |
| T15 | Account takeover through the email links, or enumeration of registered addresses | Reset and verify links are 32 random bytes, stored only as SHA-256, single use (atomic consume), expire (30 min and 24 h), tied to a purpose, and a newer link replaces the older; the token travels in the URL fragment so it never reaches a server log or Referer; the reset request answers identically for unknown addresses and sends mail in the background; weak passwords do not burn the link; a reset revokes every session | 7 of the 12 mutation-check rules, `test_security.py` |
| T16 | A hostile uploaded file (XML entity bomb, file-reading entity, zip bomb, huge file) or a poisoned value | `defusedxml` refuses entities and external references; upload 50 MB, decompressed 400 MB, 3,000,000 records and 200,000 CSV rows caps; every value passes the same range checks as manual entry (rejected, never clamped); nothing is stored until the owner confirms, and the preview is readable only by its owner for 15 minutes | 4 of the 16 mutation-check rules, `test_healthimport.py`, `test_imports_api.py` |
| T18 | A held answer (health content) read by someone who should not, or a decision made by someone who should not | Only the `clinician` role reaches `/v1/clinician/`; the queue list carries no content; a case never shows who the person is; opening a case and every decision are audited (ids and decision, never the note); a decision is accepted once, for a pending case, with CSRF; the case is in the person's export and erased with their account | `test_clinician_api.py`; mutation-check rule 'any signed-in person can open held answers' (caught); live check over HTTP (D85) |
| T20 | Unwanted or manipulated nudges: spam, nudges at night, a nudge nobody opted into, someone else's inbox, text injected through a model or a number | Off until the person opts in; policy in code (quiet hours in the person's own timezone, 2 a day, 4 h gap, growing cooldown) that Delivery checks AGAIN before the inbox; the model only picks from a closed set and never writes text; wording is fixed templates, numbers must be digits; timezone validated against the system database; own-data-only routes, CSRF, extra fields refused; erased with the account | `test_engagement.py`, `test_engagement_handler.py` (delivery veto, template refusal), `test_nudges_api.py`; live run with the real model (D87). Not fuzzed |
| T19 | A hostile or malformed PDF/image uploaded as a blood report (decompression bomb, huge page count, slow OCR, a PDF that tries to be another type) | Type by magic bytes, 15 MB, 6 pages, 30 M pixel and 30 s per page limits, files processed in memory only, at most 2 scans at once, 6 uploads an hour, own-data-only pending store, nothing saved until the person ticks and confirms, every saved row re-validated; a model is never used so report text cannot inject instructions | `test_lab_upload.py`; live run over HTTP with the real stack (D86). Not fuzzed |
| T21 | The signing secret read from an environment file, leaked in a log, or sent over plain http; a stolen cookie over an unencrypted link | The API can load its signing secret and the Google token key from Azure Key Vault with the VM's managed identity (`VITALMESH_AUTH_SECRET_KEYVAULT`): no stored credential, vault name and secret name validated before they go into a URL, fixed hosts, schemes checked, no token or secret in any message, and a vault failure stops the process (no fallback to a random or file secret). An https `VITALMESH_BASE_URL` turns Secure cookies and HSTS on, and https with `VITALMESH_SECURE_COOKIES=0` refuses to start | `libs/accounts/tests/test_keyvault.py` (22), `services/api/tests/test_https_rule.py` (4), bandit; checked on two real Azure VMs (secret read, wrong name and bad reference fail closed) and over TLS 1.3 with HSTS and Secure cookies (D92). The certificate used was self-signed: a public certificate is untested |
| T17 | Someone connects THEIR Google account to MY account (login CSRF), or steals the stored Google permission | the sign-in `state` is random, single use, 10 minutes, stored under the signed-in person and checked at the callback; PKCE binds the code to the browser that started it; only the refresh token is stored, encrypted (Fernet); one scope; disconnect and account deletion revoke at Google; no token in logs, errors or the audit trail | 4 of the 20 mutation-check rules, `test_google_api.py` |

## 5. Known gaps (stated, not hidden)

- **Email verification and password reset exist, but there is no mail service.** The local build writes the links to the server log, so a real person cannot receive one; a deployment needs an SMTP or mail-API sender, and `LogMailer` (which puts a token in a log) must not run with real users. Timing of the reset answer is equalised by sending in the background, not measured.
- **Clinician accounts are created by an operator with a script and the role is the only control** (no multi-factor, no assignment or locking between clinicians, no check that a clinician is real). A stolen clinician account reads held answers (not who asked) and can decide them: the audit log would show it, no alert would fire.
- **The operator console (`/ops`) relies on the operator role alone** (no network boundary, no multi-factor). It shows flow, timing and counts and the server removes question and answer text before it leaves, but an operator account is a powerful credential: a stolen one reads every person's run flow (not their content).
- **No multi-factor authentication.**
- **Google Calendar was verified live once** (connect with PKCE and event creation worked against the real Google); the revoke endpoint on Disconnect and the expiry paths are not yet exercised live. In Testing mode Google expires the permission after 7 days and shows an unverified-app warning.
- **HTTPS was checked once, with a self-signed certificate.** An https base URL now forces Secure cookies and HSTS, and the signing secret can come from Key Vault (T21), but a public certificate (needs a domain name and an open port 80), certificate renewal and a web application firewall are not built or tested. The local build still runs on plain http.
- **The input screen is weak** (a blocklist). The real defence is that the model cannot act: its output is verified and gated.
- **No penetration test or external review.** The mutation checks prove our tests notice 21 removed security rules, and (Phase 7, in progress) mutations of the Safety Guardian's own code; it does not prove there are no other holes.
- **Erasure leaves a few short-lived, shared traces in Redis** (D89): the aggregator's per-run state (up to 24 h, specialist advice keyed by run), the shared LLM response cache (7 days, keyed by a prompt hash, not erasable per person), idempotency keys (event ids only) and rate-limit keys (an IP or email, at most an hour). Everything keyed by the person is erased through a per-person index, checked by a test that scans the whole keyspace; before 2026-11-07 the processor's derived health state was missed.
- **Account layer measured per process, not at scale.** Sign-in rate (argon2id, 12-17 per second per core) and request rates were measured on one laptop (D81, D82); the event pipeline and the agent graph were load-tested and chaos-tested (docs/SCALING.md), but the account store was not run at a million people.
- **Browser behaviour is tested by hand once, not yet by an automated browser suite or an accessibility audit.**
- **Model provider sees question text.** Only synthetic or public data is ever sent; a real deployment needs a data-processing agreement and a decision on regional hosting.

## 6. How this is kept honest

Each row above names its test. A threat with no test goes in section 5. The red-team phase will attack this list and add what it finds.
