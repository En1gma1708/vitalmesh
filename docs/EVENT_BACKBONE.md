# EVENT BACKBONE — Kafka as the agents' nervous system

Status: DRAFT v0.1. Phase 4 and 5b add no event types: the API publishes `question.asked` after the ingress screen, and a `tracker` consumer group records every T3 topic into a Redis run log for the app (no effect on a run). Supersedes the "in-process T3 graph" idea in ARCHITECTURE.md section 3. Sections 3 and 10-13 are implemented for the deterministic T0 path (Phases 0-1); the agent events in section 4 are implemented for the T3 run in Phase 3 (D68) with two deviations: one `advice.completed` payload carries a `specialist` field instead of one event type per specialist, and there is no `evidence.attached` (Evidence is a tool inside each specialist). Topics: questions.asked, runs.planned, advice.completed, advice.bundles, recommendations.drafted, drafts.cleared, reviews.completed, reviews.requested, reviews.decided. The Aggregator is built with Redis state (the Quix Streams spike was not done).

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
`event_id` (idempotency key), `correlation_id` (one recommendation run), `causation_id` (event that caused this), `user_id` (partition key), `household_id` (tenancy), `source` + `external_id` (provider dedupe: the idempotency key is (source, external_id) when present, else `event_id`), `type`, `schema_version`, `occurred_at`, `trace_id`, `tier`, `deadline_at`, payload. Schemas in Schema Registry (Protobuf), backward-compatible only; CI blocks breaking changes.

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
- At-least-once + idempotent handlers: dedupe on `(source, external_id)` when present, else `event_id`, with a three-state store (acquire, commit, release) so a failed attempt never poisons its key. Handlers are written to be retry-safe (idempotent state updates, deterministic output ids); transactional producers are reserved for steps that cannot be made idempotent.
- One retry topic per backoff tier (`<topic>.retry.<n>`, constant delay each, so no head-of-line blocking), then the DLQ; poison messages never block a partition. DLQ records keep the original bytes for replay (replay tool: later phase).
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

## 9. Decisions and open items
- **Decided:** Protobuf with a Schema Registry (backward compatibility); one `Envelope` message with a `oneof` payload per topic.
- **Open:** the Aggregator (fan-in join with a deadline): Kafka-native in Python (Quix Streams) after a spike, with a Redis-state fallback.
- **Demo idea:** show an unchanged producer gaining a new consumer (for example a "Hydration Coach" agent) live, the strongest proof of the decoupling claim.

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

## 11. Failure contract (structured, retry-aware)
Every `*.failed` event and DLQ record uses one schema: `code`, `category` (`invalid_input`, `integrity_mismatch`, `timeout`, `cancelled`, `resource_constraint`, `unavailable`, `execution_failure`, `safety_blocked`), `message`, `retryable` (bool), `remediation.action`, `details`. Retry topics only receive `retryable=true`; everything else goes straight to the DLQ with its remediation hint. Partial facts are preserved on failure (what was computed before it failed), never discarded.

## 12. Event schema conventions (Phase 0 draft, Protobuf)
Derived from the observed data models of two real open-source apps (wger and Mealie); both used published API schemas as ground truth.
- **Explicit units:** every amount carries a unit; never assume grams or metres.
- **`metric_type` registry:** one extensible `Measurement` message with a registered `metric_type`, not one message per metric; free-form extras live in a typed, validated `extra_data` map.
- **Sessions have start and end timestamps,** not a single time.
- **Subjective feedback is its own event** (`feedback.recorded`, e.g. how a workout felt); it feeds the Engagement and Coach roles.
- **Payload families under `user.event.received`:** `measurement.recorded`, `meal.logged`, `workout.session.completed`, `sleep.session.recorded`, `feedback.recorded`, `mealplan.entry.changed`, `lab.result.recorded`.
- **Lab results:** a result is either a number with an explicit unit or a text value (never both); the lab's reference text is kept verbatim and parsed into low/high only when it is a simple interval or one-sided limit; anything else is unclassified, never guessed.
- **Recommendations:** `recommendation.released` (topic `recommendations.released`, only the Guardian-checked path publishes) and `recommendation.blocked` (topic `safety.blocked`) carry claims with provenance, the engine version and the rules version.
- **Sleep, heart rate and vitals** have no open-app reference; derive them from public wearable API documentation, not from the apps above.
- **Windowed reads:** read APIs take a date window plus pagination; consumers never request unbounded history.
- **Bounded evidence payloads:** payloads have an explicit size budget and report `unavailable: <reason>` instead of truncating silently (same rule as the failure contract in section 11).

## 13. Delivery semantics (implemented and tested in Phase 0)
- Offsets commit only after processing returned, and a retry or DLQ publish is flushed before the commit, so a crash anywhere re-delivers the record.
- A record whose retry delay has not elapsed waits in its tier's consumer (`x-not-before-ms` header); each tier is consumed separately.
- Expired events (`deadline_at` passed) go straight to the DLQ; they are never retried.
- An event whose idempotency key is in flight elsewhere is retried (bounded); a duplicate that already completed is skipped.
- A Schema Registry outage propagates instead of dead-lettering: consumption stalls (visible as lag) and resumes when the registry returns.
- Consumers use a 10 s session timeout so a hard-killed worker is replaced quickly (the 45 s default stalled restarts in testing).
