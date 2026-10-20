"""Entry point: ``uv run python -m vitalmesh_api`` (env: VITALMESH_KAFKA_BOOTSTRAP,
VITALMESH_SCHEMA_REGISTRY_URL, VITALMESH_REDIS_URL, VITALMESH_API_PORT)."""

from __future__ import annotations

import os

import redis
import uvicorn
from fastapi import FastAPI
from vitalmesh_common.kafka import KafkaPublisher, KafkaSettings, SchemaRegistrySerde, ensure_topics
from vitalmesh_t0.profiles import RedisProfileStore

from vitalmesh_api.app import create_app


def build_app() -> FastAPI:
    settings = KafkaSettings.from_env()
    ensure_topics(settings)
    client = redis.Redis.from_url(os.environ.get("VITALMESH_REDIS_URL", "redis://127.0.0.1:6379/0"))
    return create_app(
        RedisProfileStore(client),
        SchemaRegistrySerde(settings),
        KafkaPublisher(settings, client_id="api"),
    )


def main() -> None:
    uvicorn.run(
        build_app(), host="127.0.0.1", port=int(os.environ.get("VITALMESH_API_PORT", "8000"))
    )


if __name__ == "__main__":
    main()
