"""Lab result history. The implementation moved to ``vitalmesh_stores`` (D68); this module keeps
the old import path."""

from vitalmesh_stores.labstore import InMemoryLabStore, LabStore, RedisLabStore

__all__ = ["InMemoryLabStore", "LabStore", "RedisLabStore"]
