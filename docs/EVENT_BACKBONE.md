# EVENT BACKBONE — Kafka as the agents' nervous system

Status: DRAFT v0.1. Supersedes the "in-process T3 graph" idea in ARCHITECTURE.md section 3. Planning only.

## 1. Core idea
Agents never call each other. Each agent **consumes events, does its work, publishes a fact** about what it finished. Producers do not know who consumes their output.

Why (vs direct calls):
| Direct agent-to-agent calls | Kafka events |
|---|---|
| Slow agent blocks the caller | Producer publishes and moves on; consumers work when ready |
| Downstream outage = caller must retry/handle | Event is durably stored; consumer catches up after recovery |
| Adding a consumer means editing the producer | New consumer group subscribes to the same topic; zero changes upstream |
| Hard to scale one agent independently | Each agent is its own consumer group, scaled on its own lag |
| No history | Log is replayable: re-run a new prompt/model version over past events for evals |

Example from the brief, in our domain: `nutrition.advice.completed` is consumed **independently** by Evidence (to find citations), Synthesizer (to merge), and Memory Curator (to learn preferences). Nutrition knows about none of them.

## 2. Roles: Kafka between agents, LangGraph inside agents
- **Between agents:** Kafka (choreography). 
- **Inside one agent:** LangGraph (tool-calling loop, bounded retries, structured output) and LangChain model/tool abstractions.
- **Tracing:** Langfuse; trace ID carried in the event envelope so one recommendation = one trace across all agents.
- **Human-in-the-loop:** event-based (`review.requested` / `review.decided`), so no worker is held waiting; durable by construction.

## 3. Event envelope (every message)
`event_id` (idempotency key), `correlation_id` (one recommendation run), `causation_id` (event that caused this), `user_id` (partition key), `type`, `schema_version`, `occurred_at`, `trace_id`, `tier`, `deadline_at`, payload. Schemas in Schema Registry (Protobuf), backward-compatible only; CI blocks breaking changes.

## 4. Event catalogue (past-tense facts)
| Event | Published by | Consumed by (independently) |
|---|---|---|
| `user.event.received` | API | State Processor |
| `health.state.updated` | State Processor | Triage, Memory Curator, Anomaly (T0) |
| `anomaly.detected` | Anomaly (T0 code) | Triage |
| `question.asked` | Concierge | Triage |
| `run.planned` (tier, specialists, budget, deadline) | Triage | Context Assembler, Aggregator, specialists |
| `context.assembled` | Context Assembler | the selected specialists |
| `nutrition.advice.completed` / `movement.advice.completed` / `condition.advice.completed` | specialists | Evidence, Aggregator, Memory Curator |
| `evidence.attached` | Evidence | Aggregator |
| `advice.bundle.ready` (all expected parts, or partial on deadline) | Aggregator | Synthesizer |
| `recommendation.drafted` | Synthesizer | Safety Guardian |
| `safety.cleared` / `safety.blocked` | Guardian | Checker (if risk high) or Release; Synthesizer repair (blocked, max 1) |
| `review.completed` | Checker | Release / Escalation Briefer |
| `review.requested` / `review.decided` | Briefer / Human console | Human console / Release |
| `recommendation.released` | Release (code) | Engagement, Delivery (SSE), Audit, Eval sampler |
| `nudge.planned` -> `notification.sent` | Engagement -> Delivery | Audit, Engagement |
| `schedule.requested` (user opted in, plan has sessions) | Concierge / Release | Scheduler |
| `schedule.proposed` (slots from free/busy) | Scheduler | Delivery (asks user), Guardian (schedule rules: recovery days, sleep window) |
| `schedule.confirmed` / `schedule.declined` | Concierge (user action) | Scheduler, Audit |
| `calendar.event.created` / `calendar.event.failed` | Scheduler | Delivery, Audit |
| `*.failed` / `*.dlq` | any | Ops tooling, replay |

Topics are per pipeline stage (event `type` header for routing), keyed by `user_id`, to keep partition count manageable and per-user ordering intact.

## 5. Parallelism and fan-in (the hard part of choreography)
Specialists run in parallel because they consume the same `run.planned`/`context.assembled`. The **Aggregator** (code, stateful stream join keyed by `correlation_id`) knows from `run.planned` which parts to expect and emits `advice.bundle.ready` when all arrive **or when `deadline_at` passes** (partial bundle, flagged degraded). A slow or dead specialist therefore never stalls the run indefinitely.

## 6. Reliability rules
- At-least-once + idempotent handlers (dedupe on `event_id` in Redis/Postgres). Consume-transform-produce uses transactional producers where the step is not naturally idempotent.
- Retry topics with backoff, then DLQ; poison messages never block a partition. Replay tool for DLQ.
- Per-agent consumer groups, KEDA scaling on that group's lag; LLM rate limits enforced per agent with a token bucket, so one provider quota can't starve others.
- Deadlines on every run; expiry produces a degraded-but-safe result (fall back to T1/T0 recommendation).
- Safety Guardian sits on the only path to `recommendation.released`; nothing else can publish that event (ACLs on the topic).

## 7. Honest trade-offs (and mitigations)
| Cost of choreography | Mitigation |
|---|---|
| Flow is implicit, harder to debug | Correlation/causation IDs, single Langfuse trace per run, auto-generated flow diagram from event catalogue |
| Extra hop latency (ms) | Negligible vs LLM seconds; T0 path stays in the processor with no agent hops |
| Eventual consistency for the user | Client subscribes by `correlation_id` over SSE for progress/final result |
| Spaghetti risk as consumers grow | Event catalogue is a reviewed contract; each event has an owner |
| Failure of fan-in | Deadline + partial bundle semantics |

## 8. Why this serves the 1M-user goal
Backpressure is natural (lag), bursts are absorbed by the log, each stage scales independently, one agent's outage degrades gracefully instead of failing requests, and the T0/T1 tiers skip the agent hops entirely.

## 9. Open decisions
- D4: Aggregator via Kafka Streams-style state vs Redis-backed worker (Python-friendly; leaning Redis + deadline timer topic).
- D5: Protobuf (matches your gRPC/Protobuf experience) vs Avro.
- D6: Demo shows an unchanged producer gaining a new consumer (e.g. a "Hydration Coach" agent) live — strongest proof of the decoupling claim.

## 10. Safety at every boundary (defense in depth)
One final gate is not enough: unsafe output would burn tokens through Evidence/Synthesizer first, and individually safe parts can be unsafe combined. So:

| Boundary | Checks (all deterministic code unless noted) | Failure action |
|---|---|---|
| Ingress (`user.event.received`, `question.asked`) | schema, PII redaction, prompt-injection screening, rate limits | reject / DLQ |
| On consume (every agent) | envelope + schema + version + deadline; do not trust upstream flags (zero-trust) | `*.failed` / DLQ |
| On publish (every agent) | output schema; KG contraindication pre-check; claim needs citation or computed source | `safety.blocked`, no downstream spend |
| Final gate (Guardian) | full rules + KG on the merged recommendation; Checker agent (LLM) only above risk threshold | repair x1 or drop; high risk -> human review |

Rules:
- **Cheap checks everywhere, expensive checks by risk.** LLM checks never run on every hop.
- **One shared, versioned rules library** imported by every agent; rule version is stamped in the event envelope so events checked under old rules can be detected and reprocessed.
- **Fail closed.** A check that errors or cannot run blocks the event; it never passes through.
- **Release is exclusive.** Only the Guardian/Release path may publish `recommendation.released` (topic ACLs + producer identity on each event).
- The final gate stays authoritative because composition risk (e.g. nutrition + activity + medication) only appears on the merged bundle.
