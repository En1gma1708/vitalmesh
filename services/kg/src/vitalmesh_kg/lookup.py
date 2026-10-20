"""Precompiled lookup for the hot path: the graph is compiled once into Redis.

The T0 path never queries Neo4j. It reads one JSON document (the compiled edge list plus the
graph version) and builds a ``ConstraintIndex`` from it in memory.
"""

from __future__ import annotations

import json

import redis
from vitalmesh_wellness.knowledge import KG_VERSION, ConstraintIndex, Edge

KEY = "kg:constraints"


def save_index(client: redis.Redis, edges: tuple[Edge, ...], key: str = KEY) -> None:
    doc = {
        "version": KG_VERSION,
        "edges": [
            {"kind": e.kind, "condition": e.condition, "target": e.target, "source": e.source}
            for e in edges
        ],
    }
    client.set(key, json.dumps(doc, sort_keys=True))


def load_index(client: redis.Redis, key: str = KEY) -> ConstraintIndex | None:
    raw = client.get(key)
    if raw is None:
        return None
    doc = json.loads(raw)
    return ConstraintIndex(
        Edge(e["kind"], e["condition"], e["target"], e["source"]) for e in doc["edges"]
    )
