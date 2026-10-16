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
ALB -> API Gateway svc (FastAPI, stateless, autoscaled)  --SSE/WS-->  Delivery svc
   |  produce                                                  ^
   v                                                           |
Kafka (Apache Kafka KRaft; AWS mapping: MSK)  topics: user.events -> user.state -> agent.triggers -> agent.results -> recs.out ; *.dlq
   |
   +--> State Processor (consumer group; updates per-user state in Redis/Postgres; emits triggers)
   +--> Triage/Budget Agent  --> tier T0/T1/T2/T3
          T0: rules+ML+KG (no LLM)
          T1: cohort cache (Redis) + per-user deterministic personalisation
          T2: single-agent fast path (small model)
          T3: LangGraph multi-agent graph (below)
   +--> Agent Workers (consumer group, KEDA-autoscaled on lag)
   +--> Safety Guardian (always last) --> recs.out / HITL queue
Stores: Postgres+pgvector (profiles, via pgbouncer; AWS mapping: Aurora), Redis (cohort cache, idempotency, rate limits; AWS mapping: ElastiCache),
        Neo4j Community (medical knowledge graph; single node, hot path reads a precompiled Redis lookup), S3 (corpus, artifacts)
Observability: Langfuse, Prometheus/Grafana, OpenTelemetry
```

## 3. The agent flow (T3)
> **Update:** agents communicate only through Kafka events (choreography), never by direct calls. See [EVENT_BACKBONE.md](EVENT_BACKBONE.md). LangGraph is used *inside* each agent. The diagram below shows the logical order, not call edges.

Pattern mix: routing + orchestrator-workers + parallelisation + evaluator-optimizer.

```
START -> ContextAgent (load state + KG neighbourhood)
      -> TriageAgent (supervisor: picks specialists, effort budget)
      -> [Nutrition | Activity | Sleep | Condition/Medication]   (parallel fan-out, Send API)
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
| Nutrition / Activity / Sleep / Condition | LLM specialists | calculators (TDEE, zones), KG, guideline RAG |
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
At-least-once + idempotent consumers; DLQ per topic with replay tool; circuit breakers + provider fallback (LiteLLM); resumable graphs; rainbow/canary deploys so in-flight runs finish on old version; timeouts and max-step limits per run.

## 7. Security / privacy
Synthetic data only. Design hooks: JWT auth, per-user encryption keys (KMS), PII-free traces (redaction in Langfuse), audit log, consent flags, prompt-injection defence (user text never in system prompt; tool outputs treated as data).

## 8. Tech choices
Python 3.12, FastAPI, LangGraph + LangChain, LiteLLM (provider-agnostic: Claude/Groq/Cerebras), Pydantic v2, aiokafka/confluent-kafka, Postgres+pgvector, Redis, Neo4j, Langfuse, Terraform, Kubernetes (AKS/EKS) + KEDA, GitHub Actions, pytest, k6. Frontend: Next.js dashboard (demo).

## 9. Decisions log (to confirm)
- D1 Tiered compute is the core novelty. (proposed)
- Constraint: zero spend (free tiers and student credits only); cloud-portable design, see section 10.
- D3 Primary LLM: Claude for specialists/checker, small model for triage; provider via LiteLLM. (open)

## References
- Anthropic, Building Effective Agents — https://www.anthropic.com/engineering/building-effective-agents
- Anthropic, How we built our multi-agent research system — https://www.anthropic.com/engineering/multi-agent-research-system
- LangGraph docs (persistence, interrupts, Send API) — https://langchain-ai.github.io/langgraph/
- Langfuse docs — https://langfuse.com/docs

## 10. Cloud plan
| Stage | Where | Purpose |
|---|---|---|
| Dev + first load tests | Local Docker | free, fast iteration |
| Real deployment + main load tests | **Azure (student credits)** | primary cloud; real Apache Kafka on VMs/AKS |
| Optional | AWS (small remaining credits) | load generators and/or smoke deploy; proves portability |

Service mapping: Kafka -> MSK; Kubernetes AKS -> EKS; Postgres -> Aurora/RDS; Redis -> ElastiCache; secrets Key Vault -> Secrets Manager; blob -> S3. Kafka traffic never crosses clouds; only load generators may. Every benchmark is labelled local / Azure / AWS and measured vs extrapolated.
