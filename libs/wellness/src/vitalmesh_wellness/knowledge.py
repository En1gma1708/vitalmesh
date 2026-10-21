"""Seed knowledge graph of safety constraints (D54). Pure data and lookups, no I/O.

Edges link a *condition* to a constraint the Guardian and planner must respect:

* ``limits_intensity``: the condition caps exercise at a heart-rate zone (target = zone number);
* ``avoids_allergen``: the condition implies avoiding an allergen label (target = label).

This is a SEED graph written for the project: every edge is general-wellness guidance that is
widely stated, labelled ``illustrative-v1``, and not taken from a licensed or cited dataset. It
is the single source of truth: ``services/kg`` loads these same edges into Neo4j and a parity
test proves the Neo4j answers equal ``ConstraintIndex`` answers. Replace the seed with a
cited open dataset (licence checked first) before any real use.

Conditions are matched case-folded. A condition the graph does not know implies nothing; it is
never assumed safe or unsafe here (unknown conditions are a Guardian concern, not a lookup's).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

KG_VERSION = "kg-seed-1"
SOURCE = "illustrative-v1"

EdgeKind = Literal["limits_intensity", "avoids_allergen"]


@dataclass(frozen=True)
class Edge:
    kind: EdgeKind
    condition: str
    target: str  # a zone number as text for limits_intensity, an allergen label otherwise
    source: str = SOURCE


SEED_EDGES: tuple[Edge, ...] = (
    Edge("limits_intensity", "cardiac", "3"),
    Edge("limits_intensity", "uncontrolled_hypertension", "3"),
    Edge("avoids_allergen", "celiac_disease", "gluten"),
    Edge("avoids_allergen", "lactose_intolerance", "dairy"),
    Edge("avoids_allergen", "peanut_allergy", "nuts"),
    Edge("avoids_allergen", "tree_nut_allergy", "nuts"),
    Edge("avoids_allergen", "egg_allergy", "egg"),
    Edge("avoids_allergen", "fish_allergy", "fish"),
    Edge("avoids_allergen", "shellfish_allergy", "shellfish"),
    Edge("avoids_allergen", "soy_allergy", "soy"),
    Edge("avoids_allergen", "sesame_allergy", "sesame"),
)


class ConstraintIndex:
    """Precompiled lookup for the hot path (also serialised into Redis by services/kg)."""

    def __init__(self, edges: Iterable[Edge]) -> None:
        self.edges = tuple(edges)
        self._zone: dict[str, float] = {}
        self._allergens: dict[str, set[str]] = {}
        for e in self.edges:
            cond = e.condition.strip().lower()
            if e.kind == "limits_intensity":
                zone = float(e.target)
                self._zone[cond] = min(zone, self._zone.get(cond, zone))
            else:
                self._allergens.setdefault(cond, set()).add(e.target.strip().lower())

    def max_zone(self, conditions: Iterable[str]) -> float | None:
        """The strictest (lowest) zone cap among the user's conditions, or None."""
        caps = [self._zone[c] for c in _fold(conditions) if c in self._zone]
        return min(caps) if caps else None

    def implied_allergens(self, conditions: Iterable[str]) -> frozenset[str]:
        out: set[str] = set()
        for c in _fold(conditions):
            out |= self._allergens.get(c, set())
        return frozenset(out)

    def limiting_conditions(self) -> frozenset[str]:
        return frozenset(self._zone)

    def known_conditions(self) -> frozenset[str]:
        """Every condition the graph says something about. A declared condition outside this set
        is UNKNOWN to the graph, which is not the same as safe (callers must say so)."""
        return frozenset(self._zone) | frozenset(self._allergens)


def _fold(conditions: Iterable[str]) -> set[str]:
    return {c.strip().lower() for c in conditions}


DEFAULT_INDEX = ConstraintIndex(SEED_EDGES)
