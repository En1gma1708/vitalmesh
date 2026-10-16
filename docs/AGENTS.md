# AGENTS — what we build and why (no implementation)

Status: DRAFT v0.1. Companion to ARCHITECTURE.md.

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
| 1 | **Concierge** | User-facing entry for free-text questions; extracts intent, hands off to Triage | (1) free text is open input | profile read, handoff | structured Intent | small |
| 2 | **Triage/Budget** | Picks tier (T0-T3), which specialists, effort budget | (1) ambiguous cases; rules handle the clear ones first | tier policy, budget service | Plan (pydantic) | small |
| 3 | **Nutrition Specialist** | Diet guidance from intake, goals, conditions | (2)(3) own tools (food DB, calculators) | USDA lookup, calorie/macro calculators, KG | NutritionAdvice | mid |
| 4 | **Movement & Recovery Specialist** | Activity + sleep guidance (merged: they are one recovery loop; splitting would duplicate context and cost) | (2)(3) | HR-zone/sleep-debt calculators, KG | MovementAdvice | mid |
| 5 | **Condition & Medication Specialist** | Manages chronic-condition context and medication-aware advice; owns the KG | (2) deepest tool use, highest risk | KG queries, guideline RAG | ConditionAdvice + risk flags | strong |
| 6 | **Evidence Agent** | Finds guideline support, attaches citations to every claim; rejects unsupported ones | (2) retrieval loop with query rewriting | hybrid search (pgvector+BM25) | Claim->Citation map | mid |
| 7 | **Synthesizer** | Merges specialist outputs, resolves conflicts (e.g. high-intensity plan vs low-sleep flag), writes plain language | (1) judgement across conflicting inputs | none | Recommendation | strong |
| 8 | **Checker (Critic)** | Independently reviews high-risk outputs for reasoning errors/unsupported claims; may trigger ONE bounded retry | (4) independence | evidence lookup (read-only) | Verdict | strong, different prompt/model |
| 9 | **Escalation Briefer** | Builds a concise case file for the human clinician (what, why flagged, evidence, options) | (1) summarisation of messy state | state read | ReviewCase | mid |
| 10 | **Engagement Agent** (our "notification agent") | Decides *whether, when, how* to nudge (timing, fatigue, channel, tone); delivery itself is code | (1) trade-off per user history, avoid alert fatigue | notification history, quiet hours, channel prefs | NudgePlan | small |

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
Memory Curator: async consumer, writes profile facts read by Context Assembler.
```
Handoffs are real tool calls with typed (Pydantic) payloads. Each agent: objective, schema, tool list, max steps/time, token budget.

## 6. Why this decomposition (and not fewer/more)
- **Fewer** (one big agent): unbounded context, no parallelism, no independent verification, can't scale per-tier.
- **More** (agent per nutrient/organ system): coordination cost and latency exceed benefit; Anthropic notes multi-agent systems use many times more tokens, so each added agent must earn its place. That is why Activity+Sleep are merged and the Guardian is code.
- Roster is 10 runtime agents but only 3-5 run on any one request (effort scaling by Triage).

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
