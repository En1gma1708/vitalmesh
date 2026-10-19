"""Test doubles shared by every package's tests (no Kafka, no registry, no network)."""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Any

from vitalmesh.events.v1 import envelope_pb2

from vitalmesh_common import codec
from vitalmesh_common.envelope import EventMeta


class PlainSerde:
    """Unframed Protobuf: the same codec the Schema Registry serde wraps."""

    def serialize_event(self, topic: str, meta: EventMeta, payload: Any) -> bytes:
        return codec.encode(meta, payload)

    def deserialize_event(self, topic: str, data: bytes) -> tuple[EventMeta, Any]:
        return codec.decode(data)

    def serialize_dlq(self, topic: str, record: envelope_pb2.DlqRecord) -> bytes:
        data: bytes = record.SerializeToString()
        return data


@dataclass
class Published:
    topic: str
    key: str
    value: bytes
    headers: dict[str, str]


@dataclass
class RecordingPublisher:
    sent: list[Published] = field(default_factory=list)
    log: list[str] = field(default_factory=list)  # ordering of publish/flush calls
    fail_flush: bool = False

    def publish(
        self, topic: str, key: str, value: bytes, headers: dict[str, str] | None = None
    ) -> None:
        self.sent.append(Published(topic, key, value, headers or {}))
        self.log.append("publish")

    def flush(self) -> None:
        self.log.append("flush")
        if self.fail_flush:
            raise RuntimeError("broker unavailable")


def service_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    """True when something accepts TCP connections there (used to skip integration tests)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
