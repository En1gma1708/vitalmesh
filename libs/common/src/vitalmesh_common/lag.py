"""Consumer lag: how many published messages a consumer group has not yet processed (D84).

Lag is the first thing that says "the system is not keeping up": it grows when a group is slower
than the traffic or has stopped. It is read from the broker, not from our code: for each group and
partition, the group's committed offset against the partition's end offset. A group that has never
committed counts everything still on the partition as lag (so a connected group appears even
before its first commit: its assigned partitions are read as well as its committed ones). The
arithmetic (``lag_of``) is pure; the broker calls are a thin adapter (``consumer_lag``),
exercised by an integration test.
"""

from __future__ import annotations

from dataclasses import dataclass

from confluent_kafka import Consumer, ConsumerGroupTopicPartitions, TopicPartition
from confluent_kafka.admin import AdminClient

from vitalmesh_common.kafka import KafkaSettings

# Our own services only: the broker also holds every test run's throwaway groups.
SERVICE_PREFIXES = ("agent-", "state-processor", "t0-recommender")
_NO_OFFSET = -1001  # librdkafka's "no committed offset"


@dataclass(frozen=True)
class LagRow:
    group: str
    topic: str
    partition: int
    committed: int | None
    end: int
    lag: int
    members: int = 1  # live consumers in the group right now (0: the group has stopped)


def lag_of(committed: int | None, low: int, high: int) -> int:
    """Messages not yet processed. No commit yet: everything still retained counts."""
    if committed is None or committed < 0:
        return max(high - low, 0)
    return max(high - max(committed, low), 0)  # a commit older than retention is clamped to low


def consumer_lag(
    settings: KafkaSettings,
    prefixes: tuple[str, ...] = SERVICE_PREFIXES,
    timeout_s: float = 10.0,
) -> list[LagRow]:
    admin = AdminClient({"bootstrap.servers": settings.bootstrap_servers})
    listed = admin.list_consumer_groups(request_timeout=timeout_s).result().valid
    groups = [g.group_id for g in listed if g.group_id.startswith(prefixes)]
    if not groups:
        return []
    # both admin calls take ONE group at a time; issue them all first, then collect
    described = {
        g: admin.describe_consumer_groups([g], request_timeout=timeout_s)[g] for g in groups
    }
    offsets = {
        g: admin.list_consumer_group_offsets(
            [ConsumerGroupTopicPartitions(g)], request_timeout=timeout_s
        )[g]
        for g in groups
    }
    # a probe that only asks for watermarks: it never subscribes, so it joins no group
    probe = Consumer({"bootstrap.servers": settings.bootstrap_servers, "group.id": "ops-lag-probe"})
    marks: dict[tuple[str, int], tuple[int, int]] = {}
    rows: list[LagRow] = []
    try:
        for group in groups:
            desc = described[group].result()
            assigned = {
                (tp.topic, tp.partition)
                for m in desc.members
                if m.assignment is not None
                for tp in m.assignment.topic_partitions
            }
            committed = {
                (tp.topic, tp.partition): (None if tp.offset == _NO_OFFSET else int(tp.offset))
                for tp in offsets[group].result().topic_partitions
            }
            for key in sorted(assigned | committed.keys()):
                if key not in marks:
                    marks[key] = probe.get_watermark_offsets(
                        TopicPartition(*key), timeout=timeout_s, cached=False
                    )
                low, high = marks[key]
                done = committed.get(key)
                rows.append(
                    LagRow(
                        group,
                        key[0],
                        key[1],
                        done,
                        high,
                        lag_of(done, low, high),
                        len(desc.members),
                    )
                )
    finally:
        probe.close()
    return sorted(rows, key=lambda r: (r.group, r.topic, r.partition))
