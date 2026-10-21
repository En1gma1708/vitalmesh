# PRD — VitalMesh: Event-Driven Multi-Agent Health Recommendation Platform

Status: DRAFT v0.1. Requirements are stable; implementation status is in the README (Phases 0-2 done; Phase 3, the multi-agent graph over Kafka, is built and under measurement).

## 1. Problem
Health recommendation apps are either (a) static rule engines that ignore a user's live signals, or (b) a chatbot that calls an LLM per request. (b) is unsafe (hallucinated medical claims) and cannot scale: 1M users x LLM-per-event is economically and operationally impossible.

## 2. Goal
A production-grade, **multi-agent** health recommendation system that:
1. Produces personalised, evidence-grounded, safety-checked recommendations (nutrition, activity, sleep, chronic-condition/medication-aware).
2. Is **event-driven on Kafka** and sustains **>= 1,000,000 registered users with >= 100,000 concurrent active** (10% peak concurrency) on a cloud deployment (Azure; see ARCHITECTURE.md section 10).
3. Uses **graphs** twice: LangGraph agent graphs for orchestration, and a **medical knowledge graph** for deterministic contraindication reasoning.
4. Is observable (Langfuse), evaluated (offline evals + LLM-judge), and demo-able.

## 3. Novelty (the pitch to the professor)
**"Tiered Agentic Compute"** — agents are expensive, so the system decides *whether an agent is needed at all*.
- T0 Deterministic (rules + ML + graph traversal): ~95% of events. No LLM.
- T1 Cohort cache: users map to a health-state archetype; a recommendation generated once is reused by the whole cohort after per-user deterministic personalisation + safety re-check.
- T2 Single small-model agent: routine refresh.
- T3 Full multi-agent graph: anomalies, new conditions, complex questions.
A **Triage/Budget agent** routes events to a tier and enforces per-tier LLM budgets and back-pressure. This is what makes 1M users feasible, and is measurable (LLM calls per 1k events, cost per user-month).

Second novelty: **Safety is architecture, not prompt.** Contraindications (drug-food, condition-activity) are graph queries + Python rules in the execution path; an LLM checker agent reviews only high-stakes outputs; red flags escalate to human-in-the-loop clinician review (LangGraph interrupt, resumable).

## 4. Users and use cases
- End user: gets daily plan, real-time nudges, can ask free-form questions.
- Clinician reviewer: approves/rejects escalated recommendations.
- Operator: watches cost, latency, safety metrics.

Key flows: onboarding profile -> wearable/log events stream -> recommendation refresh; anomaly (e.g. sustained high resting HR, glucose spike) -> escalation; chat question -> multi-agent answer with citations.

## 5. Functional requirements
| ID | Requirement |
|----|-------------|
| F1 | Ingest user events (vitals, meals, activity, sleep, symptoms) via API -> Kafka |
| F2 | Maintain per-user health state (stream processing, idempotent) |
| F3 | Triage agent assigns tier T0-T3 per event |
| F4 | Specialist agents: Nutrition, Activity, Sleep, Condition/Medication |
| F5 | Evidence agent: RAG over guideline corpus (WHO/ICMR/ADA etc.), every claim cited |
| F6 | Safety Guardian: deterministic rules + KG contraindication check on 100% of outputs |
| F7 | Checker/reflection agent for high-stakes outputs, bounded retry (1) |
| F8 | HITL escalation with resumable state |
| F9 | Real-time delivery of recommendations (SSE/WebSocket) |
| F10 | Full tracing of every agent run (Langfuse) |
| F11 | Eval harness with golden cases + safety red-team set |
| F12 | Concierge (v1): conversational agent for free-text questions, goal setting and coaching (motivational-interviewing style), handing off to Triage |
| F13 | Rule-driven meal planning (T0/T1): weekly plans from deterministic rules and filters (day, entry type, query filter); agents only explain or handle exceptions; free-text entries allowed |
| F14 | Scheduler (optional demo feature): propose workout slots from calendar free/busy or a tokenized iCal feed; write to a calendar only after explicit user confirmation |
| F16 | Lab results: structured entry and blood-report PDF upload (digital and scanned); every extracted value is checked against the source text and confirmed by the user; range classification and unit conversion are deterministic; wellness guidance only, critical values route to a clinician |
| F17 | Health app charts (weight, sleep, activity, calories vs target, lab markers, goal rings) in a health-only user view; engineering metrics live in a separate console |
| F15 | MCP servers (optional): Knowledge (read-only tools), Ops (read-only debugging), Calendar (user-side) |

## 6. Non-functional requirements (targets, to be *measured*, not claimed)
- Scale: 1M registered, 100k concurrent, sustained 20k events/s ingest (load-tested with synthetic users).
- Latency: T0 p95 < 150 ms; T3 p95 < 12 s streaming first token < 2 s.
- Availability: 99.9%; no single point of failure; multi-AZ.
- Safety: 0 contraindication violations on the red-team suite; 100% claims cited or computed.
- Cost: report LLM cost per active user-month; target < $0.02.
- Reliability: at-least-once Kafka + idempotent consumers; DLQ; resumable agent state.

## 7. Domain scope and exclusions
**Domain: general wellness** (nutrition, activity, sleep, stress/recovery). Declared conditions and medications are treated as *safety constraints* (what to avoid), not as disease management. The conversational **Concierge** agent is part of v1.

**Out of scope:** Diagnosis, prescribing, real PHI. Synthetic data only; disclaimer "not medical advice". Real regulatory compliance (HIPAA/DPDP) is documented as future work with design hooks (encryption, audit log, consent).

## 8. Success metrics / demo
Live demo: (1) simulator pushes events for N thousand synthetic users; (2) dashboard shows tier distribution, LLM calls avoided, lag, p95; (3) one user flows through full T3 graph with trace; (4) an unsafe recommendation is blocked by the guardian; (5) HITL approval resumes the graph.

Optional demo moment: the user asks the Concierge for a week plan, the rule-driven planner fills meals deterministically, the Scheduler proposes workout slots from calendar availability, and nothing is written to the calendar until the user confirms.

## 9. Risks
LLM quota/cost (mitigate: tiers, cache, mock LLM for load tests); claiming 1M without proof (mitigate: honest load-test methodology, capacity math in ARCHITECTURE.md); Cloud cost (mitigate: local-first dev, scripted short load-test windows, teardown scripts, budget alerts; free tiers and student credits only).

## 10. Related work
- **Google Personal Health Agent / Fitbit Gemini coach** (research.google blog "The anatomy of a personal health agent"; arXiv 2508.20148): an orchestrator routes queries to three sub-agents (data science, domain expert, health coach). It established the quality layer (multi-agent beats single agent in their expert evaluations, per secondary reports; not verified here).
- **What VitalMesh adds:** a scale layer (tiered compute so most events never need an LLM, Kafka choreography between agents), a safety layer (deterministic Guardian at every boundary, knowledge-graph contraindications, human review) and explicit measurement discipline (claims carry provenance; numbers are never LLM-produced).
- **What we borrow:** the three roles map to deterministic analytics + calculators (data science), Evidence agent + specialists (domain expert) and the Concierge with a coaching style (health coach).
