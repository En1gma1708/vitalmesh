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
RECOMMENDATIONS_RELEASED = (
    "recommendations.released"  # only the Guardian-checked path publishes here
)
SAFETY_BLOCKED = "safety.blocked"

# The T3 multi-agent run, one topic per pipeline stage (D68). Each is consumed by one agent
# group; adding a consumer group needs no change upstream.
QUESTIONS_ASKED = "questions.asked"  # API -> Triage
RUNS_PLANNED = "runs.planned"  # Triage -> specialists, Aggregator
ADVICE_COMPLETED = "advice.completed"  # specialists -> Aggregator
ADVICE_BUNDLES = "advice.bundles"  # Aggregator -> Synthesizer
RECOMMENDATIONS_DRAFTED = "recommendations.drafted"  # Synthesizer -> Guardian
DRAFTS_CLEARED = "drafts.cleared"  # Guardian -> Checker or Release
REVIEWS_COMPLETED = "reviews.completed"  # Checker -> Synthesizer (revise), Release, Briefer
REVIEWS_REQUESTED = "reviews.requested"  # Briefer -> human console
REVIEWS_DECIDED = "reviews.decided"  # human console -> Release

RETRY_INFIX = ".retry."  # one retry topic per backoff tier: <topic>.retry.<n>, n = 1..max_retries
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
    RECOMMENDATIONS_RELEASED: TopicSpec(
        name=RECOMMENDATIONS_RELEASED,
        partitions={Profile.LOCAL: 6, Profile.PRODUCTION: 96},
        retention_ms=7 * _DAY_MS,
    ),
    SAFETY_BLOCKED: TopicSpec(
        name=SAFETY_BLOCKED,
        partitions={Profile.LOCAL: 6, Profile.PRODUCTION: 48},
        retention_ms=30 * _DAY_MS,
    ),
}

# T3 stages carry far fewer events than user.events (about 0.5% of traffic, ARCHITECTURE s5)
# and each event costs seconds of agent time, so they get fewer partitions.
for _name, _prod in (
    (QUESTIONS_ASKED, 24),
    (RUNS_PLANNED, 24),
    (ADVICE_COMPLETED, 48),
    (ADVICE_BUNDLES, 24),
    (RECOMMENDATIONS_DRAFTED, 24),
    (DRAFTS_CLEARED, 24),
    (REVIEWS_COMPLETED, 24),
    (REVIEWS_REQUESTED, 12),
    (REVIEWS_DECIDED, 12),
):
    TOPICS[_name] = TopicSpec(
        name=_name,
        partitions={Profile.LOCAL: 3, Profile.PRODUCTION: _prod},
        retention_ms=7 * _DAY_MS,
    )


def retry_topic(name: str, tier: int) -> str:
    """Retry topic for backoff tier ``tier`` (1-based).

    One topic per tier keeps each topic's delay constant, so messages stay ordered by
    ``not_before`` and a long delay can never block a short one (no head-of-line blocking).
    """
    if tier < 1:
        raise ValueError("retry tier is 1-based")
    return f"{name}{RETRY_INFIX}{tier}"


def dlq_topic(name: str) -> str:
    return f"{name}{DLQ_SUFFIX}"


def base_topic(name: str) -> str:
    """Strip a retry-tier or DLQ suffix to get the originating topic."""
    if name.endswith(DLQ_SUFFIX):
        return name[: -len(DLQ_SUFFIX)]
    head, sep, tail = name.rpartition(RETRY_INFIX)
    if sep and tail.isdigit():
        return head
    return name


def all_topic_specs(retry_tiers: int = 3) -> list[TopicSpec]:
    """Every declared topic plus its retry-tier and DLQ companions (same partitioning)."""
    specs: list[TopicSpec] = []
    for spec in TOPICS.values():
        specs.append(spec)
        for tier in range(1, retry_tiers + 1):
            specs.append(
                TopicSpec(
                    name=retry_topic(spec.name, tier),
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
