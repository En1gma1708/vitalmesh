"""The seed graph in a real Neo4j: load, read back, parity with the in-code index, Redis lookup."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import redis
from neo4j import Driver, GraphDatabase
from vitalmesh_kg.graph import clear_graph, load_edges, read_edges
from vitalmesh_kg.lookup import load_index, save_index
from vitalmesh_wellness.knowledge import DEFAULT_INDEX, SEED_EDGES

pytestmark = pytest.mark.integration

CONDITIONS = sorted({e.condition for e in SEED_EDGES} | {"unheard_of", "flu"})


@pytest.fixture()
def driver() -> Iterator[Driver]:
    d = GraphDatabase.driver("bolt://127.0.0.1:7687")
    clear_graph(d)  # the local graph holds only the seed; start every test from empty
    yield d
    d.close()


def test_load_is_idempotent_and_round_trips(driver: Driver) -> None:
    load_edges(driver, SEED_EDGES)
    first = read_edges(driver)
    load_edges(driver, SEED_EDGES)  # twice: MERGE must not duplicate anything
    second = read_edges(driver)
    assert first == second
    assert set(first) == set(SEED_EDGES) and len(first) == len(SEED_EDGES)
    with driver.session() as s:
        nodes = s.run("MATCH (n) RETURN count(n) AS n").single()
        rels = s.run("MATCH ()-[r]->() RETURN count(r) AS n").single()
    assert rels is not None and rels["n"] == len(SEED_EDGES)
    assert nodes is not None and nodes["n"] > 0


def test_graph_answers_equal_the_in_code_index(driver: Driver) -> None:
    from vitalmesh_wellness.knowledge import ConstraintIndex

    load_edges(driver, SEED_EDGES)
    from_graph = ConstraintIndex(read_edges(driver))
    for cond in CONDITIONS:
        assert from_graph.max_zone({cond}) == DEFAULT_INDEX.max_zone({cond}), cond
        assert from_graph.implied_allergens({cond}) == DEFAULT_INDEX.implied_allergens({cond}), cond
    assert from_graph.max_zone(set(CONDITIONS)) == DEFAULT_INDEX.max_zone(set(CONDITIONS))
    assert from_graph.implied_allergens(set(CONDITIONS)) == DEFAULT_INDEX.implied_allergens(
        set(CONDITIONS)
    )


def test_cypher_query_for_a_condition(driver: Driver) -> None:
    load_edges(driver, SEED_EDGES)
    with driver.session() as s:
        rows = s.run(
            "MATCH (:Condition {name: $c})-[:AVOIDS]->(a:Allergen) RETURN a.name AS a",
            c="celiac_disease",
        ).data()
    assert [r["a"] for r in rows] == ["gluten"]


def test_redis_lookup_reproduces_the_index(driver: Driver, redis_client: redis.Redis) -> None:
    load_edges(driver, SEED_EDGES)
    key = f"it-{uuid.uuid4().hex[:8]}:kg"
    save_index(redis_client, read_edges(driver), key=key)
    try:
        loaded = load_index(redis_client, key=key)
        assert loaded is not None
        for cond in CONDITIONS:
            assert loaded.max_zone({cond}) == DEFAULT_INDEX.max_zone({cond})
            assert loaded.implied_allergens({cond}) == DEFAULT_INDEX.implied_allergens({cond})
    finally:
        redis_client.delete(key)
    assert load_index(redis_client, key=key) is None  # absent key is reported, not guessed
