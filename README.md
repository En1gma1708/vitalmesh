# VitalMesh

An event-driven, multi-agent health recommendation platform built on **Kafka + LangGraph + Langfuse**, designed for 1M+ users. Cloud-portable: runs locally in Docker, deployed and load-tested on Azure, with an AWS mapping (see [Architecture](docs/ARCHITECTURE.md)). Domain: general wellness.

**Core idea — Tiered Agentic Compute:** most events are handled deterministically (rules, ML, knowledge-graph reasoning); a cohort cache serves similar users; LLM agents are reserved for cases that need them. A pure-code Safety Guardian checks every recommendation, and high-risk cases go to human review.

**Status:** Phase 0 (foundation) and Phase 1 (deterministic core) are implemented and tested: event envelope and failure contracts, Protobuf schemas, an at-least-once processor with retry tiers and a dead-letter queue, wellness calculators, a safety Guardian with typed claim provenance that fails closed, a rule engine, a rule-driven meal planner whose allergen and diet checks the Guardian re-derives, lab results with unit conversion and range classification, an anomaly check, measurement history, a health-only chart page, a seed knowledge graph in Neo4j, a T0 service that turns state updates into Guardian-checked recommendations, and a FastAPI gateway (about 312 unit and 20 integration tests). Measured on one machine: the in-process T0 recommendation over HTTP has p95 of about 3.5 ms; the two-hop Kafka chain has p95 of 13-15 ms for spaced events and about 430 ms for a simultaneous burst of 50 through one consumer thread per stage. Phase 2 has started: an LLM router over free-tier providers is in; agents (LangGraph), evals and cloud deployment follow.

Design docs: [PRD](docs/PRD.md), [Architecture](docs/ARCHITECTURE.md), [Agents](docs/AGENTS.md), [Event backbone](docs/EVENT_BACKBONE.md).

## Quick start
```bash
uv sync
uv run python scripts/gen_proto.py          # generate Protobuf code
uv run ruff check . && uv run mypy libs services conftest.py
uv run pytest -m "not integration"          # unit tests

docker compose -f infra/docker-compose.yml up -d --wait   # Kafka, Schema Registry, Redis, Neo4j (localhost only)
uv run pytest -m integration                # end-to-end tests against the real stack
uv run python -m vitalmesh_processor        # run the state processor
uv run python -m vitalmesh_t0               # run the T0 recommendation service
uv run python scripts/seed_demo.py demo-1   # synthetic demo user
uv run python -m vitalmesh_api              # API; open http://127.0.0.1:8000/app/demo-1
docker compose -f infra/docker-compose.yml down -v
```
The integration tests reset the topics in the topic contract on the local stack; do not point them at anything you care about.

## Layout
`libs/common` shared contracts and Kafka adapters · `libs/wellness` calculators, claims, Guardian, rule engine · `libs/llm` LLM router (fallback, rate limits, cache, mock) · `services/processor` state processor · `services/t0` T0 recommendation service · `services/api` FastAPI gateway and chart page · `services/kg` knowledge graph loader · `proto` event schemas · `infra` local stack · `scripts` code generation

**Highlights:** agents communicate only through Kafka events; a Safety Guardian (pure code) checks every boundary; rule-driven meal planning on the deterministic tier; an optional calendar Scheduler that never writes without user confirmation; lab results and blood-report PDF upload with user confirmation of every extracted value.

**Related work:** Google's Personal Health Agent (see [PRD](docs/PRD.md#10-related-work)). VitalMesh adds the scale and safety layers.

> Synthetic data only. Not medical advice.
