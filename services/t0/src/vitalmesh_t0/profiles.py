"""Profile read model for the T0 path. The implementation moved to ``vitalmesh_stores`` (D68) so
agents can read it without importing a service; this module keeps the old import path."""

from vitalmesh_stores.profiles import (
    InMemoryProfileStore,
    ProfileRecord,
    ProfileStore,
    RedisProfileStore,
)

__all__ = ["InMemoryProfileStore", "ProfileRecord", "ProfileStore", "RedisProfileStore"]
