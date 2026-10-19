"""Thin confluent-kafka adapters for the protocols in ``processor.py``.

Delivery guarantees (docs/EVENT_BACKBONE.md section 6):
- the producer is idempotent with ``acks=all``; ``flush`` raises if any message failed;
- the consumer disables auto-commit and commits only after ``EventProcessor.process``
  returned, i.e. after any retry/DLQ publish was acknowledged (at-least-once);
- a Schema Registry outage is NOT turned into a DLQ record: it propagates, the offset is
  not committed, and the record is redelivered once the registry is back.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
from confluent_kafka.admin import AdminClient, NewTopic
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.protobuf import ProtobufDeserializer, ProtobufSerializer
from confluent_kafka.serialization import MessageField, SerializationContext, SerializationError
from google.protobuf.message import DecodeError
from vitalmesh.events.v1 import envelope_pb2

from vitalmesh_common import codec
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.failure import EnvelopeError
from vitalmesh_common.processor import EventProcessor, Outcome, Record
from vitalmesh_common.topics import Profile, TopicSpec, all_topic_specs

log = logging.getLogger("vitalmesh.kafka")


@dataclass(frozen=True)
class KafkaSettings:
    bootstrap_servers: str = "127.0.0.1:9092"
    schema_registry_url: str = "http://127.0.0.1:8081"
    profile: Profile = Profile.LOCAL

    @classmethod
    def from_env(cls) -> KafkaSettings:
        return cls(
            bootstrap_servers=os.environ.get("VITALMESH_KAFKA_BOOTSTRAP", cls.bootstrap_servers),
            schema_registry_url=os.environ.get(
                "VITALMESH_SCHEMA_REGISTRY_URL", cls.schema_registry_url
            ),
            profile=Profile(os.environ.get("VITALMESH_PROFILE", Profile.LOCAL.value)),
        )


class KafkaDeliveryError(RuntimeError):
    """One or more published messages were not acknowledged by the broker."""


# ------------------------------------------------------------------------- publisher
class KafkaPublisher:
    def __init__(self, settings: KafkaSettings, client_id: str = "vitalmesh") -> None:
        self._producer = Producer(
            {
                "bootstrap.servers": settings.bootstrap_servers,
                "client.id": client_id,
                "enable.idempotence": True,  # no duplicates from producer retries
                "acks": "all",
                "compression.type": "lz4",
                "linger.ms": 5,
            }
        )
        self._errors: list[str] = []

    def _on_delivery(self, err: KafkaError | None, msg: Any) -> None:
        if err is not None:
            self._errors.append(f"{msg.topic()}[{msg.partition()}]: {err}")

    def publish(
        self, topic: str, key: str, value: bytes, headers: dict[str, str] | None = None
    ) -> None:
        kafka_headers = [(k, v.encode()) for k, v in (headers or {}).items()]
        for _ in range(2):
            try:
                self._producer.produce(
                    topic,
                    key=key.encode(),
                    value=value,
                    headers=kafka_headers,
                    on_delivery=self._on_delivery,
                )
                break
            except BufferError:  # local queue full: drain delivery callbacks, then retry once
                self._producer.poll(1.0)
        else:
            raise KafkaDeliveryError(f"local producer queue full publishing to {topic}")
        self._producer.poll(0)

    def flush(self, timeout: float = 30.0) -> None:
        remaining = self._producer.flush(timeout)
        errors, self._errors = self._errors, []
        if remaining:
            raise KafkaDeliveryError(f"{remaining} message(s) not delivered within {timeout}s")
        if errors:
            raise KafkaDeliveryError("; ".join(errors))


# ----------------------------------------------------------------------------- serde
class SchemaRegistrySerde:
    """Confluent wire format (magic byte + schema id) with Protobuf, per-topic subjects."""

    def __init__(self, settings: KafkaSettings, auto_register: bool = True) -> None:
        client = SchemaRegistryClient({"url": settings.schema_registry_url})
        conf = {"auto.register.schemas": auto_register}
        self._event_ser = ProtobufSerializer(envelope_pb2.Envelope, client, conf)
        self._dlq_ser = ProtobufSerializer(envelope_pb2.DlqRecord, client, conf)
        self._event_de = ProtobufDeserializer(envelope_pb2.Envelope, {}, client)

    def serialize_event(self, topic: str, meta: EventMeta, payload: Any) -> bytes:
        env = codec.to_envelope(meta, payload)
        data = self._event_ser(env, SerializationContext(topic, MessageField.VALUE))
        assert data is not None
        return bytes(data)

    def deserialize_event(self, topic: str, data: bytes) -> tuple[EventMeta, Any]:
        try:
            env = self._event_de(data, SerializationContext(topic, MessageField.VALUE))
        except (SerializationError, DecodeError, ValueError) as exc:
            raise EnvelopeError(f"undecodable envelope: {exc}") from exc
        if env is None:
            raise EnvelopeError("empty message value")
        return codec.from_envelope(env)

    def serialize_dlq(self, topic: str, record: envelope_pb2.DlqRecord) -> bytes:
        data = self._dlq_ser(record, SerializationContext(topic, MessageField.VALUE))
        assert data is not None
        return bytes(data)


# ---------------------------------------------------------------------------- topics
def ensure_topics(settings: KafkaSettings, specs: Sequence[TopicSpec] | None = None) -> list[str]:
    """Create any missing topics (idempotent). Returns the names created."""
    admin = AdminClient({"bootstrap.servers": settings.bootstrap_servers})
    specs = list(specs) if specs is not None else all_topic_specs()
    existing = set(admin.list_topics(timeout=15).topics)
    wanted = [
        NewTopic(
            s.name,
            num_partitions=s.partitions_for(settings.profile),
            replication_factor=s.replication_for(settings.profile),
            config={"retention.ms": str(s.retention_ms)},
        )
        for s in specs
        if s.name not in existing
    ]
    created: list[str] = []
    for name, future in admin.create_topics(wanted).items() if wanted else []:
        try:
            future.result()
            created.append(name)
        except KafkaException as exc:
            if exc.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS:  # lost a creation race
                raise
    return created


# ---------------------------------------------------------------------------- consume
def build_consumer(settings: KafkaSettings, group: str) -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": settings.bootstrap_servers,
            "group.id": group,
            "enable.auto.commit": False,  # commit only after processing (at-least-once)
            "auto.offset.reset": "earliest",
            "partition.assignment.strategy": "cooperative-sticky",
            "max.poll.interval.ms": 300_000,  # must exceed the longest retry-tier delay
            # A hard-killed worker is replaced within ~10 s instead of the 45 s default.
            "session.timeout.ms": 10_000,
            "heartbeat.interval.ms": 3_000,
        }
    )


def _to_record(msg: Any) -> Record:
    headers = {k: (v.decode() if v is not None else "") for k, v in (msg.headers() or [])}
    key = msg.key().decode() if msg.key() is not None else None
    return Record(msg.topic(), msg.partition(), msg.offset(), key, msg.value() or b"", headers)


def run_consumer(
    consumer: Consumer,
    topics: Sequence[str],
    processor: EventProcessor,
    stop: threading.Event,
    poll_timeout: float = 1.0,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Poll, wait out any retry delay, process, then commit the offset. Closes on exit."""
    consumer.subscribe(
        list(topics),
        on_assign=lambda _c, parts: log.info(
            "assigned %s", [(p.topic, p.partition) for p in parts]
        ),
        on_revoke=lambda _c, parts: log.info("revoked %s", [(p.topic, p.partition) for p in parts]),
    )
    try:
        while not stop.is_set():
            msg = consumer.poll(poll_timeout)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                raise KafkaException(msg.error())
            rec = _to_record(msg)
            not_before = rec.not_before()
            while not_before is not None and not stop.is_set():
                remaining = not_before.timestamp() - now()
                if remaining <= 0:
                    break
                sleep(
                    min(remaining, 1.0)
                )  # retry tiers have constant delay: no head-of-line blocking
            if stop.is_set():
                break  # uncommitted: redelivered after restart
            outcome = processor.process(rec)
            # Normal processing is debug-level: at the target rate it would drown the logs.
            log.log(
                logging.DEBUG if outcome is Outcome.PROCESSED else logging.INFO,
                "%s %s[%d]@%d",
                outcome.value,
                rec.topic,
                rec.partition,
                rec.offset,
            )
            consumer.commit(message=msg, asynchronous=False)
    finally:
        consumer.close()
