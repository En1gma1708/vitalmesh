# VitalMesh

An event-driven, multi-agent health recommendation platform built on **Kafka + LangGraph + Langfuse**, designed for 1M+ users. Cloud-portable: runs locally in Docker, deployed and load-tested on Azure, with an AWS mapping (see [Architecture](docs/ARCHITECTURE.md)). Domain: general wellness.

**Core idea — Tiered Agentic Compute:** most events are handled deterministically (rules, ML, knowledge-graph reasoning); a cohort cache serves similar users; LLM agents are reserved for cases that need them. A pure-code Safety Guardian checks every recommendation, and high-risk cases go to human review.

**Status:** Phase 0 (foundation) is implemented and tested: event envelope and failure contracts, Protobuf schemas, an at-least-once processor with retry tiers and a dead-letter queue, a state-processor service, a local Kafka stack and CI. Agents, the knowledge graph and the API gateway come in later phases.

Design docs: [PRD](docs/PRD.md), [Architecture](docs/ARCHITECTURE.md), [Agents](docs/AGENTS.md), [Event backbone](docs/EVENT_BACKBONE.md).

## Quick start
```bash
uv sync
uv run python scripts/gen_proto.py          # generate Protobuf code
uv run ruff check . && uv run mypy libs services conftest.py
uv run pytest -m "not integration"          # unit tests

docker compose -f infra/docker-compose.yml up -d --wait   # Kafka, Schema Registry, Redis (localhost only)
uv run pytest -m integration                # end-to-end tests against the real stack
uv run python -m vitalmesh_processor        # run the state processor
docker compose -f infra/docker-compose.yml down -v
```
The integration tests reset the topics in the topic contract on the local stack; do not point them at anything you care about.

## Layout
`libs/common` shared contracts and Kafka adapters · `services/processor` state processor · `proto` event schemas · `infra` local stack · `scripts` code generation

**Highlights:** agents communicate only through Kafka events; a Safety Guardian (pure code) checks every boundary; rule-driven meal planning on the deterministic tier; a calendar Scheduler that never writes without user confirmation; lab results and blood-report PDF upload with user confirmation of every extracted value.

**Related work:** Google's Personal Health Agent (see [PRD](docs/PRD.md#10-related-work)). VitalMesh adds the scale and safety layers.

> Synthetic data only. Not medical advice.
