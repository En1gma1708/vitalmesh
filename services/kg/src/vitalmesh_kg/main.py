"""Entry point: ``uv run python -m vitalmesh_kg`` loads the seed graph into Neo4j, reads it back,
and compiles the Redis lookup from what Neo4j returned (so Redis always reflects the graph).

Env: VITALMESH_NEO4J_URL (default bolt://127.0.0.1:7687), VITALMESH_REDIS_URL.
"""

from __future__ import annotations

import os

import redis
from neo4j import GraphDatabase
from vitalmesh_wellness.knowledge import SEED_EDGES

from vitalmesh_kg.graph import load_edges, read_edges
from vitalmesh_kg.lookup import save_index


def main() -> None:
    driver = GraphDatabase.driver(os.environ.get("VITALMESH_NEO4J_URL", "bolt://127.0.0.1:7687"))
    client = redis.Redis.from_url(os.environ.get("VITALMESH_REDIS_URL", "redis://127.0.0.1:6379/0"))
    try:
        load_edges(driver, SEED_EDGES)
        edges = read_edges(driver)
    finally:
        driver.close()
    save_index(client, edges)
    print(f"loaded {len(edges)} edges into Neo4j and compiled the Redis lookup")


if __name__ == "__main__":
    main()
