# VitalMesh runbook

How to start, check, stop and repair the system as it is built today. Everything here was run on a development laptop with synthetic people; nothing has run in production. Where a tool does not exist yet, this page says so under **Gaps**.

> Rules that apply to every step: never put real health data or real people into the stack (the local build writes email links to the API log, see [THREAT_MODEL.md](THREAT_MODEL.md)); keys live only in the gitignored `.env`; the model providers only ever see synthetic or public text.

## 1. Start, check, stop (local)

Prerequisites: Docker Desktop running, `uv`, a `.env` with provider keys (only needed for the agent graph).

| Step | Command | Healthy when |
|---|---|---|
| Infrastructure | `docker compose -f infra/docker-compose.yml up -d --wait` | five containers healthy: Kafka, Schema Registry, Redis, Postgres, Neo4j |
| Generate code (once) | `uv sync` then `uv run python scripts/gen_proto.py` | no error |
| Event processor | `uv run python -m vitalmesh_processor` | log shows `consuming <topic>`; no traceback |
| API | `VITALMESH_AUTH_SECRET=<32+ random bytes> uv run python -m vitalmesh_api` | `GET /healthz` returns `{"status":"ok"}` |
| Agents (optional, spends model quota) | `uv run python scripts/reset_topics.py` **first**, then `uv run python -m vitalmesh_agents triage nutrition movement condition aggregator synthesizer guardian checker release tracker` | one line per role on start |
| Engagement (optional) | add roles `engagement delivery`; `uv run python scripts/engagement_tick.py <user>` | a notification appears for an opted-in person |

Stop in the reverse order, then `docker compose -f infra/docker-compose.yml stop` (keep data) or `down -v` (delete data). Redis runs **without persistence** in this stack, so a restart empties it: re-seed the demo person with `uv run python scripts/seed_demo.py demo-1`.

Rules learned the hard way:
- Run `scripts/reset_topics.py` before the first agent start: a new consumer group replays topic history and would spend model quota on stale questions.
- Never run `pytest -m integration` while the API, processor or agents are running: they share the Kafka topics and the tests fail (D76).
- Never run a benchmark on the development stack: use the separate bench stack (`infra/docker-compose.bench.yml`, [SCALING.md](SCALING.md)).
- Use `127.0.0.1`, not `localhost` (IPv6 stalls).

## 2. Where to look when something is wrong

| Symptom | Look here | Likely cause and fix |
|---|---|---|
| Page loads but charts stay empty | `GET /v1/users/<me>/history`; processor log | processor not running (events accepted with 202 but nothing writes them): start it |
| Question never finishes | operator console `/ops` (flow view) or `GET /v1/ops/runs/<correlation_id>`; `uv run python scripts/demo_t3.py ... 25 <correlation_id>` replays the event trail | an agent role is not running, the model is rate-limited (free tiers), or the run hit its deadline and closed as partial |
| Answers say partial or "a clinician needs to look" | the answer's `partial_note`; `/clinician` queue | by design: deadline reached or the Guardian/Checker held it. Release it with `scripts/review.py <recommendation_id> approve\|reject` or the clinician page |
| A question ends with "I can't give this answer" | `uv run python scripts/trail_summary.py --since <iso>` (reads the event trail from Kafka and prints each helper's status and the Guardian rule names) | a refusal with the rule `has_claims` means no helper contributed any claim (the model was unreachable and nothing fell back); since D100 a helper answers from its pure-code tools when no model is reachable. Completion rate of real questions: `scripts/measure_answers.py` |
| Everything slow | `GET /v1/ops/lag` (consumer lag per group) | a role is down or slow. Lag that never shrinks on one group points at that role |
| Model quota exhausted | `/v1/ops/tiers`; Redis key `load:model-tokens` | the load monitor sheds work to cheaper tiers (T1/T0); wait for the window, or add a key. Simulate load without spending quota: `redis-cli zadd load:model-tokens <epoch> sim:12200` and delete the key afterwards |
| 429 on sign-in or sign-up | API log | rate limit or lockout (more than 10 sign-in attempts for one email or 30 from one address in 10 minutes; 10 sign-ups an hour from one address). Wait, or clear the `rl:*` keys in Redis on a development stack |
| Consumer group stuck, no commits | broker log; `docker compose logs kafka` | the Kafka 4.0.0 coordinator bug KAFKA-19888 (the dev stack still runs 4.0.0; the bench stack runs 4.2.2). Restart the broker, or upgrade the image |
| `integration` tests fail oddly | `docker ps`; are API/agents running? | stop them and re-run (see rules above) |

## 3. Failed and dead-lettered events

Handlers are idempotent and retry through three delay tiers (5 s, 30 s, 2 min) before a record goes to `<topic>.dlq` with its original bytes (design: [EVENT_BACKBONE.md](EVENT_BACKBONE.md) section 6, diagram 4 in [DIAGRAMS.md](DIAGRAMS.md)). Expired events (their `deadline_at` has passed) go straight to the dead-letter topic and are never retried.

To inspect (in Git Bash on Windows prefix with `MSYS_NO_PATHCONV=1`; topics are listed with `kafka-topics.sh --list`): `docker exec vitalmesh-kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic <topic>.dlq --from-beginning --max-messages 5`. Each record carries a failure code and category (EVENT_BACKBONE section 11). A **crash** loses nothing: offsets commit only after processing, and a worker that dies mid-event is recovered after its 60-second lease through the retry tiers (measured: about 2.5 minutes late, nothing lost, [SCALING.md](SCALING.md)).

## 4. People and data

| Task | How |
|---|---|
| Create an operator or clinician | `VITALMESH_NEW_PASSWORD=... uv run python scripts/create_account.py <email> --role operator` (or `clinician`). Roles are never grantable through the API |
| Export a person's data | the person signs in and uses `GET /v1/me/export` |
| Erase a person | `DELETE /v1/me` with their password. Erasure walks the per-person Redis index and removes accounts, profile, history, labs, nudges and tokens; a few short-lived shared traces remain (THREAT_MODEL gaps) |
| Reset a forgotten password | the person uses the reset link. The local build writes the link to the API log (no mail service) |
| Backfill the Redis index after an upgrade | `uv run python scripts/backfill_person_index.py` (add `--apply` to write) |

## 5. Safety checks to run before any change to the rules

| Change | Run | Must be |
|---|---|---|
| Anything in the Safety Guardian or meal planner | `uv run python scripts/mutation_guardian.py` | 0 open survivors (about 5 minutes) |
| Any access-control or API change | `uv run python scripts/mutation_check_security.py` (about 7 minutes), then, with the stack, API and processor running, `uv run python scripts/redteam_access.py` | 21 of 21 caught; 46 of 46 checks held |
| Any change that affects answers | `uv run python -m vitalmesh_evals --explainer template --check-baseline` (also with `--guard`) | "matches the baseline". Never loosen a grader or widen an answer key without a written reason in the decision log |
| Dashboard HTML | `uv run pytest services/api/tests/test_dashboard_escaping.py` and look at the page in a real browser | passes; no markup from data becomes an element |
| Everything | `uv run ruff check . && uv run ruff format --check . && uv run mypy libs services evals conftest.py && uv run pytest -m "not integration"` | clean |

CI runs these on every push except the live-model evals (quota-bound) and the load tests (manual).

## 6. Cloud (Azure)

Only used for measurement and for short, announced demos, never as a standing service. Both setups need **the owner's explicit go-ahead each time** and a signed-in Azure CLI; always delete with the matching teardown script and check that nothing is left. Cost and results: [COST.md](COST.md), [SCALING.md](SCALING.md).

* **Benchmarks:** `infra/azure` (Terraform) creates one resource group (`vitalmesh-bench`) with up to three small VMs and an optional Key Vault; `infra/azure/teardown.ps1` deletes the group and purges the soft-deleted vault.
* **Demo for one reviewer (scripted, not yet applied):** `infra/azure-app` creates one resource group (`vitalmesh-demo`) with ONE 2-vCPU, 8 GB VM, a static address with a free DNS name, and a firewall that opens only 80 and 443 (plus SSH to the operator's address). `pwsh infra/azure-app/deploy.ps1 -DeleteBy <date>` plans; `-Apply` creates it, installs the app from the public repo, starts three services, gets an HTTPS certificate and sets one shared password (written once to a git-ignored local file). `pwsh infra/azure-app/teardown.ps1` deletes everything. Model-provider keys are copied over SSH and never stored in Terraform state; tracing is off; a banner on every page asks for made-up data only. Whether it works end to end on a real VM has not been tested yet.

## 7. Gaps (stated, not hidden)

- **No dead-letter replay tool.** Records keep their original bytes so a replay is possible, but the tool is not written; today a dead letter is inspected by hand.
- **No backups.** Redis has no persistence here and Postgres has only a Docker volume. A real deployment needs snapshots, point-in-time recovery and a restore drill.
- **No alerting or paging.** The operator console shows lag and flow; nothing tells a person when it is bad.
- **No on-call process, no SLOs.** The measured numbers (p50/p95, throughput) are in [SCALING.md](SCALING.md); no objectives are set against them.
- **No mail service**; verification and reset links go to the API log.
- **Autoscaling (KEDA) and canary rollouts are designs**, not deployed.
- **Human review has one console and no assignment or locking** between clinicians.
