"""Response cache. The key covers every input that can change the answer.

A hit costs no quota and no latency, which is what makes repeated eval runs free. Only
deterministic requests (temperature 0) are cached: a sampled answer is not a function of its
inputs, so replaying it would be wrong.
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

import redis

from vitalmesh_llm.types import LLMRequest, LLMResponse

CACHE_VERSION = "llm-cache-2"


def cache_key(req: LLMRequest) -> str:
    doc = {
        "v": CACHE_VERSION,
        "alias": req.alias,
        "messages": [
            (m.role, m.content, [(t.id, t.name, t.arguments) for t in m.tool_calls], m.tool_call_id)
            for m in req.messages
        ],
        "tools": [(t.name, t.description, t.parameters) for t in req.tools],
        "temperature": req.temperature,
        "max_tokens": req.max_tokens,
        "json": req.json_output,
    }
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def cacheable(req: LLMRequest) -> bool:
    return req.temperature == 0.0


class Cache(Protocol):
    def get(self, key: str) -> LLMResponse | None: ...

    def put(self, key: str, response: LLMResponse) -> None: ...


class InMemoryCache:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, key: str) -> LLMResponse | None:
        raw = self._data.get(key)
        return LLMResponse.model_validate_json(raw) if raw else None

    def put(self, key: str, response: LLMResponse) -> None:
        self._data[key] = response.model_dump_json()


class RedisCache:
    def __init__(self, client: redis.Redis, prefix: str = "llm:cache", ttl_s: int = 7 * 86400):
        self._r = client
        self._prefix = prefix
        self._ttl = ttl_s

    def get(self, key: str) -> LLMResponse | None:
        raw = self._r.get(f"{self._prefix}:{key}")
        return LLMResponse.model_validate_json(raw) if raw else None

    def put(self, key: str, response: LLMResponse) -> None:
        self._r.set(f"{self._prefix}:{key}", response.model_dump_json(), ex=self._ttl)
