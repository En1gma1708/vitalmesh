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
from collections.abc import Mapping
from pathlib import Path

import redis
import uvicorn
from fastapi import FastAPI
from vitalmesh_accounts import (
    PostgresAccountStore,
    PostgresQaHistory,
    PostgresReviewQueue,
    RedisRateLimiter,
    TokenIssuer,
    qa_export,
    review_export,
    secret_from_env,
)
from vitalmesh_common.kafka import KafkaPublisher, KafkaSettings, SchemaRegistrySerde, ensure_topics
from vitalmesh_common.lag import consumer_lag
from vitalmesh_processor.history import RedisHistoryStore
from vitalmesh_processor.labstore import RedisLabStore
from vitalmesh_stores import (
    RedisNudgeStore,
    RedisPendingImports,
    RedisRunLog,
    export_user,
    purge_user,
)
from vitalmesh_t0.profiles import RedisProfileStore

from vitalmesh_api.app import create_app
from vitalmesh_api.googlecal import GoogleClient, TokenBox, config_from_env
from vitalmesh_api.onboarding import ProfileIn
from vitalmesh_api.opsview import eval_rows, graph_of
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


def secure_cookies_from_env(env: Mapping[str, str]) -> bool:
    """Cookies are Secure (and HSTS is sent) whenever the public address is https (D92).

    ``VITALMESH_SECURE_COOKIES=1`` still forces it on for a TLS-terminating proxy that talks plain
    http to this process. An https address with ``VITALMESH_SECURE_COOKIES=0`` is refused at start:
    that combination sends session cookies over an unencrypted link by mistake."""
    base = env.get("VITALMESH_BASE_URL", "http://127.0.0.1:8000")
    flag = env.get("VITALMESH_SECURE_COOKIES", "")
    if base.lower().startswith("https://"):
        if flag == "0":
            raise SystemExit(
                "VITALMESH_BASE_URL is https but VITALMESH_SECURE_COOKIES=0: refusing to start "
                "(session cookies must be Secure behind https)"
            )
        return True
    return flag == "1"


def build_app() -> FastAPI:
    settings = KafkaSettings.from_env()
    ensure_topics(settings)
    client = redis.Redis.from_url(os.environ.get("VITALMESH_REDIS_URL", "redis://127.0.0.1:6379/0"))
    profiles = RedisProfileStore(client)
    auth: Auth | None = None
    qa = PostgresQaHistory(DB_DSN)  # recent questions and the answers shown (D83)
    reviews = PostgresReviewQueue(DB_DSN)  # held answers waiting for a clinician (D85)
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
            purge=lambda uid: purge_user(client, uid) + qa.purge(uid) + reviews.purge(uid),
            export_extra=lambda uid: {
                **export_user(client, uid),
                "questions": qa_export(qa, uid),
                "held_answers": review_export(reviews, uid),
            },
            secure_cookies=secure_cookies_from_env(os.environ),
            base_url=os.environ.get("VITALMESH_BASE_URL", "http://127.0.0.1:8000"),
            # on by default: a person must confirm their email before questions spend model quota
            require_verified=os.environ.get("VITALMESH_REQUIRE_VERIFIED", "1") != "0",
        )
        log.warning("email links are written to this log (dev mailer); no mail service is set up")
    base_url = os.environ.get("VITALMESH_BASE_URL", "http://127.0.0.1:8000")
    google_cfg = config_from_env(base_url)
    if google_cfg is None:
        log.warning("GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET not set: Calendar connection is off")
    return create_app(
        profiles,
        SchemaRegistrySerde(settings),
        KafkaPublisher(settings, client_id="api"),
        labs=RedisLabStore(client),
        history=RedisHistoryStore(client),
        runs=RedisRunLog(client),
        imports=RedisPendingImports(client),
        nudges=RedisNudgeStore(client),
        google=GoogleClient(google_cfg) if google_cfg else None,
        token_box=TokenBox.from_env(),
        qa=qa,
        reviews=reviews,
        lag=lambda: consumer_lag(settings),
        graph=lambda: graph_of(client.get("kg:constraints")),
        evals=lambda: eval_rows(
            Path(os.environ.get("VITALMESH_EVAL_BASELINES", "evals/baselines"))
        ),
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
