# ARCHITECTURE — VitalMesh

Status: DRAFT v0.1. Design principles follow Anthropic's *Building Effective Agents* and *Multi-Agent Research System* write-ups and LangGraph production guidance (see References).

## 1. Principles
1. **Workflow first, agent where needed.** Fixed paths are code; LLM autonomy only in specialists and triage of ambiguous cases.
2. **Deterministic guardrails outside the prompt.** Safety rules and numbers are Python/graph queries, never LLM output.
3. **Compute tiers.** The cheapest mechanism that is safe wins.
4. **Stateless executors, durable state.** Any worker can resume any run (checkpointer).
5. **Idempotent everything.** Kafka is at-least-once.
6. **Trace everything.** Langfuse spans per agent/tool call; eval before deploy.

## 2. Logical view
```
Clients (web/mobile/wearables)
   |  HTTPS
Load balancer -> API Gateway svc (FastAPI, stateless, autoscaled)  --SSE/WS-->  Delivery svc
   |  produce                                                  ^
   v                                                           |
Kafka (Apache Kafka KRaft)  topics: user.events -> user.state -> agent.triggers -> agent.results -> recs.out ; *.dlq
   |
   +--> State Processor (consumer group; updates per-user state in Redis/Postgres; emits triggers)
   +--> Triage/Budget Agent  --> tier T0/T1/T2/T3
          T0: rules+ML+KG (no LLM)
          T1: cohort cache (Redis) + per-user deterministic personalisation
          T2: single-agent fast path (small model)
          T3: LangGraph multi-agent graph (below)
   +--> Agent Workers (consumer group, KEDA-autoscaled on lag)
   +--> Safety Guardian (always last) --> recs.out / HITL queue
Stores: Postgres+pgvector (profiles, via pgbouncer), Redis (cohort cache, idempotency, rate limits),
        Neo4j Community (medical knowledge graph; single node, hot path reads a precompiled Redis lookup), object storage (artifacts; the guideline corpus is in Postgres)
Observability: Langfuse, Prometheus/Grafana, OpenTelemetry
```

## 3. The agent flow (T3)
> **Update:** agents communicate only through Kafka events (choreography), never by direct calls. See [EVENT_BACKBONE.md](EVENT_BACKBONE.md). LangGraph is used *inside* each agent. The diagram below shows the logical order, not call edges.

Pattern mix: routing + orchestrator-workers + parallelisation + evaluator-optimizer.

```
START -> ContextAgent (load state + KG neighbourhood)
      -> TriageAgent (supervisor: picks specialists, effort budget)
      -> [Nutrition | Movement & Recovery | Condition & Medication]   (parallel fan-out, independent consumers)
      -> EvidenceAgent (RAG, attaches citations)
      -> Synthesizer (merge, resolve conflicts, plain-language)
      -> SafetyGuardian (DETERMINISTIC: rules + KG contraindications)
           block -> repair (bounded x1) or drop
      -> CheckerAgent (LLM reflection, only if risk >= threshold; one bounded retry)
      -> risk high? -> HITL interrupt (clinician) -> resume
      -> END (emit recs.out)
```
Rules for agents (per Anthropic multi-agent lessons): each subagent gets explicit objective, output schema (Pydantic), tool list, and boundaries; effort-scaling rules embedded in triage (simple = 1 agent, few calls).

| Agent | Type | Tools |
|-------|------|-------|
| Context | deterministic loader | state store, KG query |
| Triage/Budget | small LLM + rules | budget service, tier policy |
| Nutrition / Movement & Recovery / Condition & Medication | LLM specialists | calculators (TDEE, zones, sleep debt), KG, guideline RAG |
| Evidence | RAG agent | pgvector + BM25 hybrid search |
| Synthesizer | LLM | none |
| Safety Guardian | **pure code** | KG contraindication queries, rule engine |
| Checker | LLM | evidence lookup |

## 4. Knowledge graph
Nodes: Condition, Drug, Food, Nutrient, Activity, Guideline, Symptom. Edges: CONTRAINDICATED_WITH, INTERACTS_WITH, RECOMMENDED_FOR, SUPPORTED_BY(guideline). Sources: openly licensed (e.g. DrugBank open subsets/openFDA, USDA FoodData, guideline text). Read-heavy and static -> contraindication closure precompiled into Redis/in-memory lookup replicated to workers, so it scales independent of user count (Neo4j Community has no read replicas).

## 5. Scale design (the 1M claim)
Assumptions (to validate by load test): 1M registered; 10% concurrent peak = 100k; 1 event / user / 30 s while active = ~3.3k events/s average, design for 20k/s burst.
- Tier mix target: T0 95%, T1 3.5%, T2 1%, T3 0.5% -> at 20k ev/s T3 = 100 runs/s. At ~10 s each = ~1,000 concurrent graph runs; async workers (I/O bound) -> ~50-100 pods. LLM provider rate limits are a first-class constraint -> queue + budget.
- Kafka: partition key = user_id (ordering per user); 96+ partitions for user.events; consumer lag drives KEDA autoscaling. Idempotency key = event_id.
- Checkpoint writes: 1 per graph node; T3 only, so ~1k writes/s via pgbouncer; Redis for hot state.
- Back-pressure: if LLM budget/lag exceeded, triage downgrades tier (graceful degradation to T1/T0) instead of failing.
- Production target (documented, Terraform plan/validate only): 3-broker Kafka across 3 zones, Kubernetes across 3 zones, Postgres with reader, Redis cluster mode. Actually deployed: scaled-down on Azure (see section 10).
- **Proof plan**: k6/Locust + Kafka producer simulator; mock-LLM mode with realistic latency distribution for scale tests; real-LLM run for quality. Report p50/p95/p99, lag, cost. Be explicit about what was measured vs extrapolated.

## 6. Reliability
At-least-once + idempotent consumers; DLQ per topic with replay tool; circuit breakers + provider fallback (own router in libs/llm); resumable graphs; rainbow/canary deploys so in-flight runs finish on old version; timeouts and max-step limits per run.

## 7. Security / privacy
Synthetic data only. Implemented in Phase 5b (D75): accounts with argon2id passwords, 15-minute signed access tokens and single-use rotating refresh tokens, a deny-by-default gate, per-person data isolation, consent, export and erase, CSRF protection, a nonce-based content security policy, rate limits and an audit log (no secrets, no health data). Still design hooks: JWT auth at scale behind a load balancer, per-user encryption keys (KMS), per-user encryption keys (KMS), PII-free traces (redaction in Langfuse), audit log, consent flags, prompt-injection defence (user text never in system prompt; tool outputs treated as data).

## 8. Tech choices
Python 3.12, FastAPI, LangGraph + LangChain, an in-house provider router on httpx (OpenAI-compatible providers: Groq, Gemini, OpenRouter), Pydantic v2, aiokafka/confluent-kafka, Postgres+pgvector (local bge-small embeddings through fastembed), Redis, Neo4j, Langfuse, Terraform, Kubernetes (AKS) + KEDA, GitHub Actions, pytest, k6. Frontend: Next.js dashboard (demo).

## 9. Settled decisions
- Tiered compute is the core novelty (T0 deterministic, T1 cohort cache, T2 single agent, T3 multi-agent).
- Kafka is the only inter-agent transport; the Aggregator is built Kafka-native after a spike (open).
- Redis is a store only (cache, idempotency, rate limits), never a bus; Neo4j Community is the knowledge-graph source of truth with a precompiled hot-path lookup.
- Protobuf with a Schema Registry (backward compatibility); real Apache Kafka in KRaft mode.
- Azure for Students is the primary cloud; Azure is the only cloud and local Docker the development environment; zero personal spend.
- LLMs through a provider router (`libs/llm`: Groq, Gemini and OpenRouter free tiers, with a rate limiter, a response cache and a mock for scale tests). Only synthetic or public data is ever sent.
- Safety checks at every agent boundary plus an authoritative final Guardian.

## 10. Cloud plan
| Stage | Where | Purpose |
|---|---|---|
| Dev + first load tests | Local Docker | free, fast iteration |
| Real deployment + main load tests | **Azure (student credits)** | primary cloud; real Apache Kafka on VMs/AKS |

Every benchmark is labelled local or Azure, and measured or extrapolated.

## 11. MCP layer (optional, stretch)
Three MCP servers, each with a narrow purpose (see AGENTS.md section 2b): **Knowledge** (read-only tools shared by specialists), **Ops** (read-only operator debugging), **Calendar** (user-side scheduling).

Calendar scheduling flow (demo feature): recommendation with workout sessions is released -> `schedule.requested` (only for opted-in users) -> Scheduler reads free/busy through Calendar MCP (minimal scope) -> `schedule.proposed` -> deterministic schedule rules check (recovery days, sleep window, max load) -> user confirms in chat -> `schedule.confirmed` -> Scheduler creates events -> `calendar.event.created`. Nothing is written to a calendar without explicit confirmation. Calendar calls are async consumers, rate-limited per user and per provider; tokens live in the secret store. Load tests use a mock Calendar MCP server; the live demo uses a real consenting demo account.
MCP is never used between agents, for the Safety Guardian, or on T0/T1 paths.

## 12. Engineering principles adopted
Informed by studying the design docs of REA ([morluto/rea](https://github.com/morluto/rea), MIT), an open-source MCP toolkit for agents. REA is a local single-user tool, so it informs engineering discipline and tool design, not scale.
1. **Typed claim provenance.** Every claim in a recommendation is `computed`, `cited`, `inferred` or `unknown`. Unknown is never treated as safe; the Guardian fails closed on it.
2. **Layered code.** Pure domain (rules, calculators) -> contracts -> shared application workflows -> thin adapters (Kafka consumers, FastAPI, MCP). The same workflow serves every adapter.
3. **Cohort cache keys cover every result-affecting input** (user constraints, rules version, model version, prompt version, KG version) with explicit invalidation and freshness checks. Any mismatch is a miss.
4. **Measure end-to-end tokens** per recommendation per tier, including schemas, retries and extra calls; report cold vs warm; label estimates.
5. **Explicit redaction.** Sensitive values use a typed wrapper that redacts in traces and logs; secrecy is never guessed from field names.
6. **MCP servers are stateless Streamable HTTP services** (a stdio process per client does not scale), with JSON Schemas validated against real clients and contract tests.
7. **Tests:** real workflows first, then boundary integration, then goldens; mutation testing on Guardian rules; lifecycle regressions (deadline expiry, cancellation during cleanup, retry of failed cleanup); unverified coverage is reported explicitly.
8. **Side-effect capabilities fail closed** and need explicit confirmation (calendar writes).

## 13. Meal planning on the tiers
Studied from a real app (Mealie, AGPL-3.0, via runtime and static analysis; its published OpenAPI schemas were the ground truth).
- **T0 deterministic planner:** PlanRules {day (weekday or unset), entry_type, query_filter} fill a week's slots; a random-pick endpoint chooses within the filter. No LLM on this path. Guardian checks every suggested entry against the user's constraints.
- **MealPlanEntry:** date, entry_type in {breakfast, lunch, dinner, side, snack, drink, dessert}, title, text, recipe_id (nullable), recipe_scale. Free-text entries (no recipe) are first-class.
- **Agents' role:** the Nutrition specialist explains choices and handles exceptions the rules cannot; it does not generate the numbers.
- **Reads are windowed and paginated** (start_date, end_date, page, per_page); read models follow the same shape.
- **Tenancy:** every record carries household_id and user_id.
- **Calendar:** the Scheduler can publish a tokenized read-only iCal feed (no OAuth write scope held by us), with Calendar MCP write as the opt-in upgrade.

## 13b. Accounts and security (Phase 5b, D75)
* **Identity:** `libs/accounts` (users, sessions, profiles, consent, audit in Postgres; an in-memory twin for tests). A person's id is random (`u-` + 16 hex) and the session, never the URL, says who they are.
* **Gate:** `services/api/security.py` refuses every `/v1` route without a valid session except a short public list; `/v1/users/{id}/...` needs the session's own id (others get 404), operator routes need the operator role (an operator sees internals, not a person's health data), and a route nobody wrote a rule for is refused.
* **Sessions:** the access token (HS256, algorithm pinned) lives 15 minutes; the refresh token is random, stored only as a hash, single use, and replaying a used one revokes the session; logout revokes at once, server-side.
* **Browser safety:** `SameSite` cookies, a double-submit CSRF token on state changes, a per-response script nonce in the content security policy, no CORS, `no-store` caching.
* **Abuse limits:** per-address and per-email login limits, account lockout, sign-up limits, 20 questions an hour per person (each can spend model quota).
* **Data:** Postgres is the source of truth for accounts and profiles; the Redis profile cache the agents read is refilled from it at start; export and delete reach Postgres and every Redis read model.
* **Honest limits:** the load balancer address is not trusted for rate limits; password reset and email verification need an email service; HTTPS and a managed secret store are Phase 6; real health regulation is out of scope; no independent security review has been done.

## 14. Implementation status (as of Phase 5b)
Built: T0 (deterministic) path on Kafka; the LLM router; a guideline corpus in Postgres + pgvector (local bge-small embeddings, hybrid full-text plus vector search with rank fusion); three LLM agents, each a LangGraph graph with hard limits and code-verified output: the Evidence agent (cited, quote-checked answers), the Nutrition specialist (tool calling; every number must come from a tool result) and the Concierge (input screen, closed-set intent routing, coaching); Langfuse Cloud tracing across them (traces are nested, hashed user ids, synthetic or public text only). Also built in Phase 3 (local, not yet published): the T3 multi-agent graph over Kafka: rules-based Triage with risk from the user's facts, three parallel specialists, an Aggregator (fan-in by run id with a deadline and partial bundles, Redis state), a Synthesizer, a Guardian gate that runs at drafting and again at release, an independent Checker for medium and high risk, and a Release gate whose human approval is a LangGraph interrupt checkpointed in Postgres, plus a counterfactual replay and an ablation harness. Also built in Phase 4 (local, being finished): the T1 cohort cache and the load signal with T0 shedding (see the tier section): Triage plans a cache hit as T1, a `cohort` consumer refills the cached template from the user's own deterministic tools and sends it through the unchanged Guardian gate and release, and a `cachefill` consumer learns from released answers; the cache is eligible only for low-risk users with no declared conditions, medications or flagged labs. Also built in Phase 5b: the interactive health app (ask with live steps, log data, sign-up, sign-in and onboarding pages, export and delete) on the accounts layer above. Not built: Google Calendar, the clinician and engineering consoles, cloud deployment. The Concierge and the Evidence agent are still in-process; the ablation (8 cases, one run each, re-run after fixing a claim-builder bug) did not show the full graph beating a single agent with tools: it delivered 5 answers against the single agent's 6 at about 2.4 times the model calls; its value is risk routing, review and human approval.

## 15. Delivery semantics as implemented (Phase 0)
Verified by unit tests and by integration tests against a real Kafka, Schema Registry and Redis.
- **At-least-once, no loss:** auto-commit is off; the offset commits only after processing returned, and a retry or DLQ publish is flushed before that. A failed flush propagates, so the record is redelivered.
- **Retry tiers:** one retry topic per backoff tier (`<topic>.retry.<n>`), each with a constant delay, so a long delay can never block a short one; a pure routing function decides retry, DLQ or expiry. DLQ records keep the original bytes for replay.
- **Idempotency is three-state** (acquire, commit, release): a failed attempt releases its key so the retry runs instead of being dropped as a duplicate; a crashed worker's in-flight lock expires. Redis runs with `noeviction` so a key can never be silently evicted.
- **Retry-safe handlers:** state updates are idempotent and order-independent; emitted events carry deterministic ids derived from the input event.
- **A registry outage is not a DLQ case:** it stalls consumption (visible as lag) and the records are redelivered when it returns; only genuinely undecodable bytes are dead-lettered.
- **Handler effects must be idempotent:** if committing the idempotency key fails after a successful handler, the lock expires and the handler may run again.

## References
- Anthropic, Building Effective Agents — https://www.anthropic.com/engineering/building-effective-agents
- Anthropic, How we built our multi-agent research system — https://www.anthropic.com/engineering/multi-agent-research-system
- LangGraph docs (persistence, interrupts, Send API) — https://langchain-ai.github.io/langgraph/
- Langfuse docs — https://langfuse.com/docs
