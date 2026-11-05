from __future__ import annotations

import uuid

import pytest
from confluent_kafka import Consumer, KafkaException, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic
from vitalmesh_common.kafka import KafkaSettings
from vitalmesh_common.lag import consumer_lag

pytestmark = pytest.mark.integration

SETTINGS = KafkaSettings()


def test_lag_is_what_was_published_and_not_yet_committed() -> None:
    admin = AdminClient({"bootstrap.servers": SETTINGS.bootstrap_servers})
    try:
        admin.list_topics(timeout=5)
    except KafkaException:
        pytest.skip("Kafka is not running")
    topic, group = f"lagtest-{uuid.uuid4().hex[:8]}", f"lagtest-group-{uuid.uuid4().hex[:8]}"
    admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])[topic].result()
    producer = Producer({"bootstrap.servers": SETTINGS.bootstrap_servers})
    for i in range(10):
        producer.produce(topic, value=str(i).encode())
    producer.flush(10)
    consumer = Consumer(
        {
            "bootstrap.servers": SETTINGS.bootstrap_servers,
            "group.id": group,
            "enable.auto.commit": False,
        }
    )
    try:
        # commit position 4 of 10 without reading: six messages are waiting
        consumer.commit(offsets=[TopicPartition(topic, 0, 4)], asynchronous=False)
        rows = [r for r in consumer_lag(SETTINGS, prefixes=("lagtest-group-",)) if r.group == group]
        assert len(rows) == 1
        assert (rows[0].topic, rows[0].committed, rows[0].end, rows[0].lag) == (topic, 4, 10, 6)
        consumer.commit(offsets=[TopicPartition(topic, 0, 10)], asynchronous=False)
        assert [
            r.lag for r in consumer_lag(SETTINGS, prefixes=("lagtest-group-",)) if r.group == group
        ] == [0]
    finally:
        consumer.close()
        admin.delete_topics([topic])
