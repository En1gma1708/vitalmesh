# AGENTS — what we build and why (no implementation)

Status: DRAFT v0.1. Companion to ARCHITECTURE.md. Implementation status (Phase 5b adds a `tracker` consumer that only records each run's events for the app, it is infrastructure and not an agent; Phase 4 adds the `cohort` and `cachefill` consumers, rules and calculators only, no model; Phase 3 below): built and tested: Concierge (1, in-process), Triage/Budget (2, rules only), Nutrition Specialist (3), Movement & Recovery (4), Condition & Medication (5), Evidence Agent (6, a tool inside each specialist), Synthesizer (7), Checker (8), Escalation Briefer (9); the specialists, Triage, Synthesizer, Checker and the Release gate run as Kafka consumers (services/agents). Not built: Engagement (10), Scheduler (11) and the background agents. Whether the multi-agent graph beats a single agent with tools: the ablation (8 cases, re-run after a bug fix) says no on delivered answers and cost; the graph's value so far is risk routing, review and human approval, not output quality.

## 1. The test an agent must pass
A component becomes an **LLM agent** only if at least one is true; otherwise it is plain code (cheaper, testable, safer):
1. **Judgement over open input** — the right action can't be enumerated in rules (free text, conflicting evidence, personalisation trade-offs).
2. **Distinct tools/context** — it needs its own toolset and a context window that would be polluted by other work (Anthropic: subagents compress context in parallel windows).
3. **Parallelisable** — independent of siblings, so fan-out cuts latency.
4. **Independence required** — a verifier must not share the author's reasoning (checker pattern).

Anything numeric, compliance-related or safety-critical is **code**, never an agent.

## 2. Runtime roster
Mapping to the research-assistant analogy: research -> Evidence; analysis -> Specialists; fact-check -> Checker + Guardian; writing -> Synthesizer; notification -> Engagement.

| # | Agent | Role | Why it is an agent (test) | Tools | Output | Model class |
|---|-------|------|---------------------------|-------|--------|-------------|
| 1 | **Concierge** | User-facing entry for free-text questions and coaching (motivational-interviewing style, goal setting, progress tracking); extracts intent, hands off to Triage | (1) free text is open input | profile read, handoff | structured Intent | small |
| 2 | **Triage/Budget** | Picks tier (T0-T3), which specialists, effort budget | (1) ambiguous cases; rules handle the clear ones first | tier policy, budget service | Plan (pydantic) | small |
| 3 | **Nutrition Specialist** | Diet guidance from intake, goals, conditions; owns the rule-driven meal planner (deterministic T0/T1 path; the LLM only explains choices and handles exceptions) | (2)(3) own tools (food DB, calculators) | USDA lookup, calorie/macro calculators, KG | NutritionAdvice | mid |
| 4 | **Movement & Recovery Specialist** | Activity + sleep guidance (merged: they are one recovery loop; splitting would duplicate context and cost) | (2)(3) | HR-zone/sleep-debt calculators, KG | MovementAdvice | mid |
| 5 | **Condition & Medication Specialist** | Manages chronic-condition context and medication-aware advice; owns the KG | (2) deepest tool use, highest risk | KG queries, guideline RAG | ConditionAdvice + risk flags | strong |
| 6 | **Evidence Agent** | Finds guideline support, attaches citations to every claim; rejects unsupported ones | (2) retrieval loop with query rewriting | hybrid search (pgvector+BM25) | Claim->Citation map | mid |
| 7 | **Synthesizer** | Merges specialist outputs, resolves conflicts (e.g. high-intensity plan vs low-sleep flag), writes plain language | (1) judgement across conflicting inputs | none | Recommendation | strong |
| 8 | **Checker (Critic)** | Independently reviews high-risk outputs for reasoning errors/unsupported claims; may trigger ONE bounded retry | (4) independence | evidence lookup (read-only) | Verdict | strong, different prompt/model |
| 9 | **Escalation Briefer** | Builds a concise case file for the human clinician (what, why flagged, evidence, options) | (1) summarisation of messy state | state read | ReviewCase | mid |
| 10 | **Engagement Agent** (our "notification agent") | Decides *whether, when, how* to nudge (timing, fatigue, channel, tone); delivery itself is code | (1) trade-off per user history, avoid alert fatigue | notification history, quiet hours, channel prefs | NudgePlan | small |
| 11 | **Scheduler Agent** (optional, demo feature) | Fits recommended workouts into the user's real calendar: reads free/busy, proposes slots, creates events only after the user confirms | (1)(2) judgement over messy availability plus its own tools (calendar via MCP) | Calendar MCP: free/busy read, event create (after confirmation) | SchedulePlan | small |

## 2b. MCP layer (optional, stretch)
MCP (Model Context Protocol) is used only where a shared tool server or an external integration is the point. Never for agent-to-agent messaging (Kafka), the Safety Guardian, or T0/T1 hot paths.
| Server | Exposes | Used by | Phase |
|---|---|---|---|
| **Knowledge MCP** (built: corpus search and source list only, D69; the specialists still use in-process retrieval) | KG lookups, wellness calculators, guideline search (stateless, read-only) | Specialists, Evidence; also pluggable into Claude Desktop for the demo | ~P2 |
| **Ops MCP** (read-only) | run lookup by correlation_id, Langfuse trace link, consumer lag, "why was this blocked?" | operators | ~P5 |
| **Calendar MCP** (user-side) | free/busy read; event create gated by user confirmation | Scheduler Agent | P5 |
Calendar fallback (lower risk): the Scheduler can instead publish a tokenized, read-only iCal feed of the user's plan (the pattern Mealie ships), so users subscribe without us holding OAuth write scope; Calendar MCP write stays the opt-in upgrade.
Rules: tool outputs are untrusted data; least-privilege scopes (free/busy only for reads); allowlisted servers with pinned tool descriptions; destructive/write tools need explicit user confirmation; OAuth tokens in the secret store, never in traces or events.

## 3. Background (async, off the request path)
| Agent | Why |
|-------|-----|
| **Memory Curator** | Periodically (Kafka-triggered) compresses a user's long event history into durable profile facts and preferences, so the T3 context stays small. Keeps cost flat as history grows. |
| **Eval Judge** (offline/CI) | LLM-as-judge on golden + red-team sets using rubric (accuracy, citation, safety, completeness) — gates deployments. |

## 4. Deliberately NOT agents (and why)
| Component | Implemented as | Reason |
|-----------|----------------|--------|
| **Safety Guardian** | Pure code, two forms: (a) a shared, versioned rules library that every agent runs on consume and publish (cheap pre-checks), and (b) the authoritative final gate before release | Safety must be deterministic, exhaustively testable, un-promptable. Checked at every boundary, authoritative at the end (see EVENT_BACKBONE.md section 10). Core design claim. |
| Context Assembler | Code | Fixed query set; no judgement |
| Anomaly detection | ML/statistics (T0) | Numeric, cheap, runs on 95% of events |
| Calculators (TDEE, zones, BMI) | Code | LLMs must not produce numbers |
| Cohort clustering/cache (T1) | Code/ML | Deterministic keys, big cost lever |
| Notification delivery | Code | Pure I/O |
| Clinician review | Human (HITL) | Accountability; LangGraph interrupt |

## 5. Interaction map
```
User text -> Concierge -> Triage -> (T0/T1: code paths, no agents)
                              \-> T2: one specialist + Guardian
                              \-> T3: [Nutrition | Movement&Recovery | Condition&Med] (parallel)
                                      -> Evidence -> Synthesizer -> Guardian(code)
                                      -> Checker (if risk>=threshold; <=1 retry)
                                      -> risk high? Briefer -> Human review -> resume
                                      -> Engagement (when/how to deliver) -> delivery (code)
                                      -> [opt] Scheduler: free/busy via Calendar MCP -> propose slots -> user confirms -> create events
Memory Curator: async consumer, writes profile facts read by Context Assembler.
```
Handoffs are real tool calls with typed (Pydantic) payloads. Each agent: objective, schema, tool list, max steps/time, token budget.

## 6. Why this decomposition (and not fewer/more)
- **Fewer** (one big agent): unbounded context, no parallelism, no independent verification, can't scale per-tier.
- **More** (agent per nutrient/organ system): coordination cost and latency exceed benefit; Anthropic notes multi-agent systems use many times more tokens, so each added agent must earn its place. That is why Activity+Sleep are merged and the Guardian is code.
- The roster is 11 runtime agents (one optional) but only 3-5 run on any one request (effort scaling by Triage).

## 7. Failure modes each design choice addresses
| Risk | Mitigation |
|------|-----------|
| Hallucinated medical claim | Evidence agent + citation requirement; Checker; unsupported claims dropped |
| Unsafe interaction | Per-hop deterministic pre-checks fail fast; final Guardian gate blocks merged-bundle risk; cannot be bypassed by prompt |
| Specialists duplicate work | Explicit boundaries in delegation (Anthropic lesson) |
| Runaway cost/loops | Max steps, token budgets, tier downgrade |
| Alert fatigue | Engagement agent + hard rate limits in code |
| Prompt injection via user text/tool output | Treated as data, typed schemas, Guardian is code |

## 8. Open questions
1. Merge Movement & Recovery vs split — decide by eval (does splitting improve quality enough to justify cost)?
2. (Decided) Concierge is in v1. Domain is general wellness: the Condition & Medication Specialist treats declared conditions/medications as safety constraints, not disease management.
3. Different model family for Checker (stronger independence) — affordable on quota?

## 9. Related work and what we borrowed
Google's Personal Health Agent uses three roles: data science, domain expert, health coach. Mapping:
| Their role | Our equivalent | Difference |
|---|---|---|
| Data science agent (numerical analysis of wearable time series) | Deterministic analytics and calculators (T0), no LLM-produced numbers | numbers come from code, so they are testable |
| Domain expert agent (grounded in authoritative sources) | Evidence agent + specialists + knowledge graph | citations required per claim; contraindications are graph queries |
| Health coach agent (multi-turn goal setting) | Concierge with a coaching style | same idea; ours hands off to tiered compute |
Our additions: Kafka choreography, tiered compute, Guardian at every boundary, human review.
