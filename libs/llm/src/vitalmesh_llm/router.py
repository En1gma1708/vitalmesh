"""The router: cache, then providers in order with rate limiting, cooldown and fallback.

Order of work for one request: (1) cache lookup for deterministic requests; (2) for each
provider in the alias chain: skip if it has no key, is cooling down, or has no rate budget;
otherwise call it; (3) on a 429, 5xx, timeout or unusable body, put the provider on a short
cooldown and move on; (4) cache and return the first good answer, or raise
``AllProvidersFailed`` listing the attempts. Prompts and keys are never logged or put in
errors; logs and stats carry provider names, token counts and timings only.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping

import httpx

from vitalmesh_llm.cache import Cache, InMemoryCache, cache_key, cacheable
from vitalmesh_llm.limiter import RateLimiter
from vitalmesh_llm.providers import CHAINS, ProviderSpec
from vitalmesh_llm.types import AllProvidersFailed, LLMRequest, LLMResponse

log = logging.getLogger("vitalmesh.llm")

COOLDOWN_RATE_LIMITED_S = 60.0
COOLDOWN_ERROR_S = 30.0
REQUEST_TIMEOUT_S = 30.0

MockFn = Callable[[LLMRequest], str]


def default_mock(req: LLMRequest) -> str:
    """Deterministic stand-in: same request, same text. Used by tests and scale tests."""
    last = req.messages[-1].content
    return f"mock answer ({len(last)} chars)"


def estimate_tokens(req: LLMRequest) -> int:
    chars = sum(len(m.content) for m in req.messages)
    return (
        math.ceil(chars / 4) + req.max_tokens
    )  # a deliberate over-estimate: reserve, don't guess low


class Router:
    def __init__(
        self,
        keys: Mapping[str, str],
        client: httpx.Client | None = None,
        cache: Cache | None = None,
        chains: Mapping[str, tuple[ProviderSpec, ...]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        mock: MockFn = default_mock,
    ) -> None:
        self._keys = keys
        self._client = client or httpx.Client(timeout=REQUEST_TIMEOUT_S)
        self._cache = cache if cache is not None else InMemoryCache()
        self._chains = dict(chains) if chains is not None else dict(CHAINS)
        self._clock = clock
        self._mock = mock
        self._limiters: dict[tuple[str, str], RateLimiter] = {}
        self._cooldown_until: dict[str, float] = {}
        self._lock = threading.Lock()
        self.stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    # ----------------------------------------------------------------------- public
    def complete(self, req: LLMRequest) -> LLMResponse:
        key = cache_key(req) if cacheable(req) else None
        if key is not None:
            hit = self._cache.get(key)
            if hit is not None:
                self.stats["cache"]["hits"] += 1
                return hit.model_copy(update={"cached": True, "latency_ms": 0.0})
            self.stats["cache"]["misses"] += 1

        resp = self._mock_call(req) if req.alias == "mock" else self._call_chain(req)
        if key is not None:
            self._cache.put(key, resp)
        return resp

    # ---------------------------------------------------------------------- internals
    def _mock_call(self, req: LLMRequest) -> LLMResponse:
        text = self._mock(req)
        self.stats["mock"]["calls"] += 1
        return LLMResponse(
            text=text,
            provider="mock",
            model="mock",
            prompt_tokens=estimate_tokens(req) - req.max_tokens,
            completion_tokens=math.ceil(len(text) / 4),
            latency_ms=0.0,
        )

    def _limiter(self, alias: str, spec: ProviderSpec) -> RateLimiter:
        # one limiter per provider+model: a model's free-tier quota is shared by all callers
        k = (spec.name, spec.model)
        with self._lock:
            if k not in self._limiters:
                self._limiters[k] = RateLimiter(spec.rpm, spec.tpm, self._clock, spec.rpd)
            return self._limiters[k]

    def _call_chain(self, req: LLMRequest) -> LLMResponse:
        attempts: list[str] = []
        for spec in self._chains[req.alias]:
            tag = f"{spec.name}/{spec.model}"
            if not self._keys.get(spec.key_env):
                attempts.append(f"{tag}: no key")
                continue
            if self._clock() < self._cooldown_until.get(tag, 0.0):
                attempts.append(f"{tag}: cooling down")
                continue
            if not self._limiter(req.alias, spec).try_acquire(estimate_tokens(req)):
                self.stats[spec.name]["rate_skipped"] += 1
                attempts.append(f"{tag}: no rate budget")
                continue
            try:
                return self._post(spec, req)
            except _Retryable as exc:
                cool = COOLDOWN_RATE_LIMITED_S if exc.status == 429 else COOLDOWN_ERROR_S
                self._cooldown_until[tag] = self._clock() + cool
                self.stats[spec.name]["errors"] += 1
                attempts.append(f"{tag}: {exc.reason}")
        raise AllProvidersFailed(attempts)

    def _post(self, spec: ProviderSpec, req: LLMRequest) -> LLMResponse:
        body: dict[str, object] = {
            "model": spec.model,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        if req.json_output:
            body["response_format"] = {"type": "json_object"}
        started = self._clock()
        try:
            r = self._client.post(
                f"{spec.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._keys[spec.key_env]}"},
                json=body,
                timeout=REQUEST_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            raise _Retryable(0, type(exc).__name__) from exc
        if r.status_code == 429 or r.status_code >= 500:
            raise _Retryable(r.status_code, f"HTTP {r.status_code}")
        if r.status_code >= 400:
            # a 400/401/403 will not fix itself on retry; skip this provider for a while
            raise _Retryable(r.status_code, f"HTTP {r.status_code}")
        try:
            data = r.json()
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise _Retryable(r.status_code, "unusable response body") from exc
        if not isinstance(text, str) or not text.strip():
            raise _Retryable(r.status_code, "empty completion")
        elapsed_ms = (self._clock() - started) * 1000
        resp = LLMResponse(
            text=text,
            provider=spec.name,
            model=spec.model,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=elapsed_ms,
        )
        s = self.stats[spec.name]
        s["calls"] += 1
        s["prompt_tokens"] += resp.prompt_tokens
        s["completion_tokens"] += resp.completion_tokens
        log.info(
            "%s %s ok %d+%d tokens",
            spec.name,
            spec.model,
            resp.prompt_tokens,
            resp.completion_tokens,
        )
        return resp


class _Retryable(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
