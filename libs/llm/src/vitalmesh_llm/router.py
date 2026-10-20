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
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._keys = keys
        self._client = client or httpx.Client(timeout=REQUEST_TIMEOUT_S)
        self._cache = cache if cache is not None else InMemoryCache()
        self._chains = dict(chains) if chains is not None else dict(CHAINS)
        self._clock = clock
        self._mock = mock
        self._sleep = sleep
        self._limiters: dict[tuple[str, str], RateLimiter] = {}
        self._cooldown_until: dict[str, float] = {}
        self._cooldown_rate_limited: set[str] = set()
        self._lock = threading.Lock()
        self.stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    # ----------------------------------------------------------------------- public
    def complete(self, req: LLMRequest, max_wait_s: float = 0.0) -> LLMResponse:
        """``max_wait_s`` > 0 lets the call wait for rate budget (eval runs) instead of failing
        over to a weaker provider; waiting happens only when every provider was skipped for
        budget alone, never after a real failure."""
        key = cache_key(req) if cacheable(req) else None
        if key is not None:
            hit = self._cache.get(key)
            if hit is not None:
                self.stats["cache"]["hits"] += 1
                return hit.model_copy(update={"cached": True, "latency_ms": 0.0})
            self.stats["cache"]["misses"] += 1

        resp = self._mock_call(req) if req.alias == "mock" else self._call_waiting(req, max_wait_s)
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

    def _call_waiting(self, req: LLMRequest, max_wait_s: float) -> LLMResponse:
        waited = 0.0
        while True:
            try:
                return self._call_chain(req, wait_primary=waited < max_wait_s)
            except AllProvidersFailed as exc:
                if not exc.budget_only or waited >= max_wait_s:
                    raise
                pause = min(max(exc.retry_after_s, 0.5), max_wait_s - waited)
                self._sleep(pause)
                waited += pause

    def _call_chain(self, req: LLMRequest, wait_primary: bool = False) -> LLMResponse:
        attempts: list[str] = []
        waits: list[float] = []
        budget_only = True
        for spec in self._chains[req.alias]:
            tag = f"{spec.name}/{spec.model}"
            if not self._keys.get(spec.key_env):
                attempts.append(f"{tag}: no key")
                continue
            remaining = self._cooldown_until.get(tag, 0.0) - self._clock()
            if remaining > 0:
                attempts.append(f"{tag}: cooling down")
                if wait_primary and budget_only and tag in self._cooldown_rate_limited:
                    waits.append(remaining)  # a 429 is a rate signal: waiting it out can work
                    break
                budget_only = False
                continue
            limiter = self._limiter(req.alias, spec)
            if not limiter.try_acquire(estimate_tokens(req)):
                self.stats[spec.name]["rate_skipped"] += 1
                attempts.append(f"{tag}: no rate budget")
                w = limiter.seconds_until(estimate_tokens(req))
                if w is None:
                    budget_only = False  # can never fit: waiting will not help
                else:
                    waits.append(w)
                    if wait_primary and budget_only:
                        break  # no real failure so far: wait for this provider, do not downgrade
                continue
            try:
                return self._post(spec, req)
            except _Retryable as exc:
                limited = exc.status == 429
                cool = max(exc.retry_after, 1.0) if limited else COOLDOWN_ERROR_S
                self._cooldown_until[tag] = self._clock() + cool
                if limited:
                    self._cooldown_rate_limited.add(tag)
                else:
                    self._cooldown_rate_limited.discard(tag)
                self.stats[spec.name]["errors"] += 1
                attempts.append(f"{tag}: {exc.reason}")
                if limited and wait_primary and budget_only:
                    waits.append(cool)
                    break  # wait out the rate limit on this provider instead of downgrading
                budget_only = False
        raise AllProvidersFailed(
            attempts, budget_only=budget_only and bool(waits), retry_after_s=min(waits, default=0.0)
        )

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
        if r.status_code == 429:
            raise _Retryable(429, "HTTP 429", _retry_after(r))
        if r.status_code >= 500:
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


def _retry_after(r: httpx.Response) -> float:
    try:
        return min(max(float(r.headers.get("retry-after", "")), 1.0), 120.0)
    except ValueError:
        return 20.0


class _Retryable(Exception):
    def __init__(self, status: int, reason: str, retry_after: float = 0.0) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.retry_after = retry_after
