"""Neo4j storage for the seed constraint graph (D54).

Model: ``(:Condition {name})-[:AVOIDS {source}]->(:Allergen {name})`` and
``(:Condition {name})-[:LIMITS_INTENSITY {source}]->(:Zone {number})``. Loading is idempotent
(MERGE), so running it twice leaves the same graph.
"""

from __future__ import annotations

from neo4j import Driver
from vitalmesh_wellness.knowledge import Edge

_LOAD_AVOIDS = """
UNWIND $rows AS row
MERGE (c:Condition {name: row.condition})
MERGE (a:Allergen {name: row.target})
MERGE (c)-[r:AVOIDS]->(a)
SET r.source = row.source
"""

_LOAD_LIMITS = """
UNWIND $rows AS row
MERGE (c:Condition {name: row.condition})
MERGE (z:Zone {number: row.zone})
MERGE (c)-[r:LIMITS_INTENSITY]->(z)
SET r.source = row.source
"""

_READ = """
MATCH (c:Condition)-[r:AVOIDS]->(a:Allergen)
RETURN 'avoids_allergen' AS kind, c.name AS condition, a.name AS target, r.source AS source
UNION ALL
MATCH (c:Condition)-[r:LIMITS_INTENSITY]->(z:Zone)
RETURN 'limits_intensity' AS kind, c.name AS condition, toString(z.number) AS target,
       r.source AS source
"""


def load_edges(driver: Driver, edges: tuple[Edge, ...]) -> None:
    avoids = [
        {"condition": e.condition, "target": e.target, "source": e.source}
        for e in edges
        if e.kind == "avoids_allergen"
    ]
    limits = [
        {"condition": e.condition, "zone": int(float(e.target)), "source": e.source}
        for e in edges
        if e.kind == "limits_intensity"
    ]
    with driver.session() as session:
        session.run(
            "CREATE CONSTRAINT condition_name IF NOT EXISTS "
            "FOR (c:Condition) REQUIRE c.name IS UNIQUE"
        ).consume()
        session.run(
            "CREATE CONSTRAINT allergen_name IF NOT EXISTS "
            "FOR (a:Allergen) REQUIRE a.name IS UNIQUE"
        ).consume()
        session.run(
            "CREATE CONSTRAINT zone_number IF NOT EXISTS FOR (z:Zone) REQUIRE z.number IS UNIQUE"
        ).consume()
        if avoids:
            session.run(_LOAD_AVOIDS, rows=avoids).consume()
        if limits:
            session.run(_LOAD_LIMITS, rows=limits).consume()


def read_edges(driver: Driver) -> tuple[Edge, ...]:
    with driver.session() as session:
        rows = session.run(_READ).data()
    return tuple(
        sorted(
            (Edge(r["kind"], r["condition"], r["target"], r["source"]) for r in rows),
            key=lambda e: (e.kind, e.condition, e.target),
        )
    )


def clear_graph(driver: Driver) -> None:
    """Test and dev helper: removes every node (the local graph holds only the seed)."""
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
