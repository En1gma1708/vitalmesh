# VitalMesh

An event-driven, multi-agent health recommendation platform built on **Kafka + LangGraph + Langfuse**, designed for 1M+ users. Runs locally in Docker; deployment and load tests are planned on Azure (see [Architecture](docs/ARCHITECTURE.md)). Domain: general wellness.

**Core idea — Tiered Agentic Compute:** most events are handled deterministically (rules, ML, knowledge-graph reasoning); a cohort cache serves similar users; LLM agents are reserved for cases that need them. A pure-code Safety Guardian checks every recommendation, and high-risk cases go to human review.

**Status:** Phases 0-3 are implemented, tested and published (CI green on the last published commit; 789 unit and 34 integration tests locally). Phase 4 (cohort cache, load shedding) is built locally and not yet published. Ablation (8 cases, one run each, keyword coverage, no quality judge, free-tier model), re-run after a bug fix: the single agent with tools delivered 6 answers, the one-specialist path 4, the full graph 5 plus one sent to a human; no arm produced a forbidden phrase; the full graph used 9.2 model calls and 12.2k tokens per case against 3.8 calls and 6.4k tokens for the single agent. The full graph did NOT deliver more or better answers than the single agent; its value is review and human approval of risky cases. The first run (before the fix) was worse for every arm: a meal-plan claim left out per-meal calories, so true numbers were blocked.

- **Phase 0-1, the deterministic core:** event envelope and failure contracts, Protobuf schemas, an at-least-once Kafka processor with retry tiers and a dead-letter queue, wellness calculators, a pure-code safety Guardian with typed claim provenance that fails closed, a rule engine, a rule-driven meal planner whose allergen and diet checks the Guardian re-derives, lab results with unit conversion and range classification, an anomaly check, measurement history, a health-only chart page, a seed knowledge graph in Neo4j, a T0 service and a FastAPI gateway. Measured on one machine: the in-process T0 recommendation over HTTP has p95 of about 3.5 ms; the two-hop Kafka chain has p95 of 13-15 ms for spaced events and about 430 ms for a simultaneous burst of 50 through one consumer thread per stage.
- **Phase 2, the first LLM agents:** an LLM router over free-tier providers (fallback, rate limits, cache, tool calling); an eval harness with 136 golden cases (deterministic graders, prompt-injection cases including held-out attacks, committed baselines, a deterministic guard, a relevance-and-tone judge limited to explanations); a guideline corpus in Postgres + pgvector with local embeddings and hybrid retrieval; an Evidence agent (LangGraph) whose every sentence is cited with document, page and licence and verified in code; a Nutrition specialist (LangGraph tool-calling loop) whose numbers must come from deterministic tools; a Concierge that screens, classifies and routes; and Langfuse tracing across all of it. The model is never trusted: citations, quotes and numbers are checked in code and a plain-facts fallback replaces a failed draft.
- **Phase 3, the multi-agent graph over Kafka (built, being measured):** a rules-based Triage that decides the specialists and the risk from the user's own conditions and labs; Nutrition, Movement & Recovery and Condition & Medication specialists as parallel consumers; an Aggregator that joins their parts by run id and closes at a deadline with a partial answer that says so; a Synthesizer whose numbers must come from the claims; a Guardian gate that runs when the draft is made and again at release; an independent Checker for medium and high risk; and human approval as a paused, resumable LangGraph run checkpointed in Postgres. Counterfactual replay and an ablation harness (single agent vs one specialist vs the full graph) are included. The ablation did not show that the graph beats one agent with tools (see the status line).
- **Phase 4 (built, being finished):** a T1 cohort cache and load shedding. A complete, low-risk answer for a user without declared conditions, medications or flagged labs is stored as a template with that user's own calculated numbers replaced by placeholders; another user of the same cohort (sex, age band, activity, goal, diet, allergens) asking the same question gets the template refilled from THEIR calculators, with no model call, through the same Guardian gate; any doubt falls back to a full run. A load signal (model-backed runs per minute) degrades the plan under pressure and, at critical load, answers low-risk questions with no model at all (T0). Measured so far only with a scripted model on the in-process bus: 92% fewer model calls on a cache-friendly workload, which shows the mechanism, not real cost or quality.
- **Next:** understand the grounding blocks and re-run the ablation, the live Phase 4 checks, a health app and engineering console, and cloud deployment and load tests on Azure (planned, not done). The 1M-user figure is a design target, not a measurement.

Design docs: [PRD](docs/PRD.md), [Architecture](docs/ARCHITECTURE.md), [Agents](docs/AGENTS.md), [Event backbone](docs/EVENT_BACKBONE.md).

## Quick start
```bash
uv sync
uv run python scripts/gen_proto.py          # generate Protobuf code
uv run ruff check . && uv run mypy libs services conftest.py
uv run pytest -m "not integration"          # unit tests

docker compose -f infra/docker-compose.yml up -d --wait   # Kafka, Schema Registry, Redis, Neo4j, Postgres+pgvector (localhost only)
uv run pytest -m integration                # end-to-end tests against the real stack
uv run python -m vitalmesh_processor        # run the state processor
uv run python -m vitalmesh_t0               # run the T0 recommendation service
uv run python scripts/seed_demo.py demo-1   # synthetic demo user
uv run python -m vitalmesh_api              # API; open http://127.0.0.1:8000/app/demo-1
uv run python scripts/demo_flow.py demo-1   # one event through Kafka, printed as a recommendation
uv run python scripts/demo_guardian.py      # unsafe recommendations blocked by the Guardian (no stack needed)
uv run python -m vitalmesh_evals --explainer template --guard   # 79 golden eval cases, no model
uv run python scripts/build_corpus.py       # guideline corpus into Postgres (downloads two public PDFs, local embeddings)
uv run python -m vitalmesh_evals --only concierge   # live agent evals (needs the corpus and an LLM key in .env)
docker compose -f infra/docker-compose.yml down -v
```
The integration tests reset the topics in the topic contract on the local stack; do not point them at anything you care about.

## Layout
`libs/common` shared contracts and Kafka adapters · `libs/wellness` calculators, claims, Guardian, rule engine · `libs/llm` LLM router (fallback, rate limits, cache, mock, tool calling) · `libs/retrieval` Postgres + pgvector corpus store, chunking, local embeddings · `libs/evidence` Evidence agent · `libs/specialists` Nutrition specialist · `libs/concierge` input screen and intent routing · `libs/tracing` Langfuse tracing · `evals` golden-case eval harness (deterministic graders, resumable runs, live agent evals) · `services/processor` state processor · `services/t0` T0 recommendation service · `services/api` FastAPI gateway and chart page · `services/kg` knowledge graph loader · `proto` event schemas · `infra` local stack · `scripts` code generation, demos, corpus build

**Highlights:** agents communicate only through Kafka events; a Safety Guardian (pure code) checks every boundary; rule-driven meal planning on the deterministic tier; an optional calendar Scheduler that never writes without user confirmation; lab results and blood-report PDF upload with user confirmation of every extracted value.

**Related work:** Google's Personal Health Agent (see [PRD](docs/PRD.md#10-related-work)). VitalMesh adds the scale and safety layers.

> Synthetic data only. Not medical advice.
