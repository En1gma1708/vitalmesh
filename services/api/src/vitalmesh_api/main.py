"""Entry point: ``uv run python -m vitalmesh_api``.

Environment: VITALMESH_KAFKA_BOOTSTRAP, VITALMESH_SCHEMA_REGISTRY_URL, VITALMESH_REDIS_URL,
VITALMESH_PG_DSN (accounts), VITALMESH_API_PORT, VITALMESH_AUTH_SECRET (at least 32 bytes; a random
one is used, with a warning, if unset), VITALMESH_SECURE_COOKIES=1 behind HTTPS.

Accounts and the sign-in gate are ON by default (D75). ``VITALMESH_AUTH=off`` serves the older,
account-free mode where the user id in the address is the identity; it exists only for the seeded
demo users and prints a warning, because anyone who can reach the port can read any profile.
"""

from __future__ import annotations

import logging
import os

import redis
import uvicorn
from fastapi import FastAPI
from vitalmesh_accounts import (
    PostgresAccountStore,
    RedisRateLimiter,
    TokenIssuer,
    secret_from_env,
)
from vitalmesh_common.kafka import KafkaPublisher, KafkaSettings, SchemaRegistrySerde, ensure_topics
from vitalmesh_processor.history import RedisHistoryStore
from vitalmesh_processor.labstore import RedisLabStore
from vitalmesh_stores import RedisRunLog, export_user, purge_user
from vitalmesh_t0.profiles import RedisProfileStore

from vitalmesh_api.app import create_app
from vitalmesh_api.onboarding import ProfileIn
from vitalmesh_api.security import Auth

log = logging.getLogger("vitalmesh.api")
DB_DSN = os.environ.get("VITALMESH_PG_DSN", "postgresql://vitalmesh@127.0.0.1:5432/vitalmesh")


def _counts(client: redis.Redis, key: str) -> dict[str, int]:
    return {
        (k.decode() if isinstance(k, bytes) else str(k)): int(v)
        for k, v in client.hgetall(key).items()
    }


def warm_profiles(store: PostgresAccountStore, cache: RedisProfileStore) -> int:
    """Refill the profile cache the agents read from the database. Redis keeps nothing across a
    restart; Postgres does, so every start (and any later call) restores everyone's profile."""
    restored = 0
    for user_id, data in store.all_profiles():
        try:
            cache.put(ProfileIn.model_validate(data).to_record(user_id))
            restored += 1
        except ValueError:
            log.warning("a stored profile no longer validates and was not cached: %s", user_id)
    return restored


def build_app() -> FastAPI:
    settings = KafkaSettings.from_env()
    ensure_topics(settings)
    client = redis.Redis.from_url(os.environ.get("VITALMESH_REDIS_URL", "redis://127.0.0.1:6379/0"))
    profiles = RedisProfileStore(client)
    auth: Auth | None = None
    if os.environ.get("VITALMESH_AUTH", "on").lower() == "off":
        log.warning("VITALMESH_AUTH=off: no sign-in, any reachable user id can be read. Demo only.")
    else:
        accounts = PostgresAccountStore(DB_DSN)
        log.info("restored %d profiles from the database", warm_profiles(accounts, profiles))
        auth = Auth(
            accounts,
            TokenIssuer(secret_from_env()),
            RedisRateLimiter(client),
            profiles,
            purge=lambda uid: purge_user(client, uid),
            export_extra=lambda uid: export_user(client, uid),
            secure_cookies=os.environ.get("VITALMESH_SECURE_COOKIES", "") == "1",
        )
    return create_app(
        profiles,
        SchemaRegistrySerde(settings),
        KafkaPublisher(settings, client_id="api"),
        labs=RedisLabStore(client),
        history=RedisHistoryStore(client),
        runs=RedisRunLog(client),
        ops=lambda: {
            "tiers": _counts(client, "tiers:count"),
            "cohort": _counts(client, "cohort:stats"),
        },
        auth=auth,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(
        build_app(), host="127.0.0.1", port=int(os.environ.get("VITALMESH_API_PORT", "8000"))
    )


if __name__ == "__main__":
    main()
