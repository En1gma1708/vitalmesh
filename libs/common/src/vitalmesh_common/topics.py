"""Topic contract: names, keys, partitions, and retry/DLQ naming.

Topics are per pipeline stage, keyed by ``user_id`` so per-user ordering holds
(docs/EVENT_BACKBONE.md section 4). Partition counts are a deployment profile: the
local profile is small, the production profile documents the capacity target
(docs/ARCHITECTURE.md section 5) and is only validated by Terraform plan, not applied.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

USER_EVENTS = "user.events"
USER_STATE = "user.state"

RETRY_SUFFIX = ".retry"
DLQ_SUFFIX = ".dlq"


class Profile(StrEnum):
    LOCAL = "local"
    PRODUCTION = "production"  # documented target, not deployed on free tiers


@dataclass(frozen=True)
class TopicSpec:
    name: str
    partitions: dict[Profile, int]
    retention_ms: int
    key: str = "user_id"
    replication: dict[Profile, int] | None = None

    def partitions_for(self, profile: Profile) -> int:
        return self.partitions[profile]

    def replication_for(self, profile: Profile) -> int:
        if self.replication is None:
            return 1 if profile is Profile.LOCAL else 3
        return self.replication[profile]


_DAY_MS = 24 * 60 * 60 * 1000

TOPICS: dict[str, TopicSpec] = {
    USER_EVENTS: TopicSpec(
        name=USER_EVENTS,
        partitions={Profile.LOCAL: 6, Profile.PRODUCTION: 96},
        retention_ms=7 * _DAY_MS,
    ),
    USER_STATE: TopicSpec(
        name=USER_STATE,
        partitions={Profile.LOCAL: 6, Profile.PRODUCTION: 96},
        retention_ms=7 * _DAY_MS,
    ),
}


def retry_topic(name: str) -> str:
    return f"{name}{RETRY_SUFFIX}"


def dlq_topic(name: str) -> str:
    return f"{name}{DLQ_SUFFIX}"


def base_topic(name: str) -> str:
    """Strip a retry/DLQ suffix to get the originating topic."""
    for suffix in (RETRY_SUFFIX, DLQ_SUFFIX):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def all_topic_specs() -> list[TopicSpec]:
    """Every declared topic plus its retry and DLQ companions (same partitioning)."""
    specs: list[TopicSpec] = []
    for spec in TOPICS.values():
        specs.append(spec)
        specs.append(
            TopicSpec(
                name=retry_topic(spec.name),
                partitions=spec.partitions,
                retention_ms=spec.retention_ms,
                key=spec.key,
                replication=spec.replication,
            )
        )
        specs.append(
            TopicSpec(
                name=dlq_topic(spec.name),
                partitions=spec.partitions,
                retention_ms=30 * _DAY_MS,
                key=spec.key,
                replication=spec.replication,
            )
        )
    return specs
