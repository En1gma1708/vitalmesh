# VitalMesh

An event-driven, multi-agent health recommendation platform built on **Kafka + LangGraph + Langfuse**, designed for 1M+ users. Cloud-portable: runs locally in Docker, deployed and load-tested on Azure, with an AWS mapping (see [Architecture](docs/ARCHITECTURE.md)). Domain: general wellness.

**Core idea — Tiered Agentic Compute:** most events are handled deterministically (rules, ML, knowledge-graph reasoning); a cohort cache serves similar users; LLM agents are reserved for cases that need them. A pure-code Safety Guardian checks every recommendation, and high-risk cases go to human review.

Status: planning. See [PRD](docs/PRD.md), [Architecture](docs/ARCHITECTURE.md), [Agents](docs/AGENTS.md), [Event backbone](docs/EVENT_BACKBONE.md).

> Synthetic data only. Not medical advice.
