from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from vitalmesh_llm import AllProvidersFailed, InMemoryCache, LLMRequest, Message, Router
from vitalmesh_llm.cache import cache_key, cacheable
from vitalmesh_llm.limiter import RateLimiter
from vitalmesh_llm.providers import CHAINS, GROQ_STRONG

SECRET = "sk-test-SECRET-do-not-leak"
KEYS = {"GROQ_API_KEY": SECRET, "GEMINI_API_KEY": SECRET + "g", "OPENROUTER_API_KEY": SECRET + "o"}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def req(text: str = "hello", **over: Any) -> LLMRequest:
    base: dict[str, Any] = {
        "messages": (Message(role="user", content=text),),
        "alias": "strong",
        "data_class": "synthetic",
        "purpose": "test",
    }
    return LLMRequest(**{**base, **over})


def ok_body(text: str = "answer") -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


class Server:
    """Scripted provider responses by host; records every call (never the key)."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = script
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls.append((host, json.loads(request.content)))
        step = self.script[host].pop(0) if len(self.script[host]) > 1 else self.script[host][0]
        if isinstance(step, Exception):
            raise step
        status, body = step
        return httpx.Response(status, json=body)


def router(
    server: Server, clock: Clock | None = None, cache: InMemoryCache | None = None
) -> Router:
    return Router(
        KEYS,
        client=httpx.Client(transport=httpx.MockTransport(server)),
        cache=cache,
        clock=clock or Clock(),
    )


GROQ, GEMINI, OPENR = "api.groq.com", "generativelanguage.googleapis.com", "openrouter.ai"


# ------------------------------------------------------------------------ happy path
def test_success_maps_fields_and_sends_a_bounded_request() -> None:
    s = Server({GROQ: [(200, ok_body("hi there"))]})
    r = router(s).complete(req(max_tokens=64))
    assert (r.text, r.provider, r.model) == ("hi there", "groq", GROQ_STRONG.model)
    assert (r.prompt_tokens, r.completion_tokens, r.cached) == (11, 7, False)
    host, body = s.calls[0]
    assert host == GROQ and body["max_tokens"] == 64 and body["temperature"] == 0.0
    assert "response_format" not in body
    assert router(Server({GROQ: [(200, ok_body())]})).complete(req(json_output=True))


def test_json_output_requests_json_mode() -> None:
    s = Server({GROQ: [(200, ok_body("{}"))]})
    router(s).complete(req(json_output=True))
    assert s.calls[0][1]["response_format"] == {"type": "json_object"}


# ---------------------------------------------------------------------------- policy
def test_data_class_is_mandatory_and_restricted() -> None:
    with pytest.raises(ValidationError):
        LLMRequest(messages=(Message(role="user", content="x"),), purpose="t")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        req(data_class="real_user")
    with pytest.raises(ValidationError):
        req(max_tokens=100_000)
    with pytest.raises(ValidationError):
        LLMRequest(messages=(), data_class="synthetic", purpose="t")


# -------------------------------------------------------------------------- fallback
@pytest.mark.parametrize(
    "first",
    [
        (429, {}),
        (500, {}),
        (503, {}),
        (401, {}),
        (200, {"choices": []}),
        (200, ok_body("  ")),
        httpx.ConnectTimeout("slow"),
    ],
)
def test_a_failing_provider_falls_through_to_the_next(first: Any) -> None:
    s = Server({GROQ: [first], GEMINI: [(200, ok_body("from gemini"))]})
    r = router(s).complete(req())
    assert (r.provider, r.text) == ("gemini", "from gemini")
    assert [c[0] for c in s.calls] == [GROQ, GEMINI]


def test_cooldown_skips_a_failed_provider_until_it_expires() -> None:
    clock = Clock()
    s = Server({GROQ: [(429, {}), (200, ok_body("groq back"))], GEMINI: [(200, ok_body("gem"))]})
    rt = router(s, clock)
    assert rt.complete(req("a")).provider == "gemini"
    assert rt.complete(req("b")).provider == "gemini"  # groq still cooling: not even tried
    assert [c[0] for c in s.calls] == [GROQ, GEMINI, GEMINI]
    clock.t += 61
    assert rt.complete(req("c")).provider == "groq"


def test_all_failing_raises_with_attempts_and_never_leaks_a_key_or_prompt() -> None:
    s = Server({GROQ: [(500, {})], GEMINI: [(429, {})], OPENR: [(502, {})]})
    with pytest.raises(AllProvidersFailed) as e:
        router(s).complete(req("my private-looking prompt"))
    text = str(e.value) + repr(e.value.attempts)
    assert SECRET not in text and "private-looking" not in text
    assert len(e.value.attempts) == 3 and "HTTP 429" in text


def test_missing_key_is_skipped_not_called() -> None:
    s = Server({GEMINI: [(200, ok_body("g"))]})
    keys = {**KEYS, "GROQ_API_KEY": ""}
    rt = Router(keys, client=httpx.Client(transport=httpx.MockTransport(s)), clock=Clock())
    assert rt.complete(req()).provider == "gemini"
    assert [c[0] for c in s.calls] == [GEMINI]


def test_rate_budget_exhaustion_moves_to_the_next_provider_without_a_call() -> None:
    clock = Clock()
    s = Server({GROQ: [(200, ok_body("g1"))], GEMINI: [(200, ok_body("gem"))]})
    rt = router(s, clock)
    big = req("x" * 20_000, max_tokens=1000)  # ~6k tokens: Groq's 8k/min fits one, not two
    assert rt.complete(big).provider == "groq"
    assert rt.complete(req("y" * 20_000, max_tokens=1000)).provider == "gemini"
    assert rt.stats["groq"]["rate_skipped"] == 1


# ----------------------------------------------------------------------------- cache
def test_deterministic_requests_are_cached_and_cost_no_second_call() -> None:
    s = Server({GROQ: [(200, ok_body("once"))]})
    rt = router(s)
    a, b = rt.complete(req()), rt.complete(req())
    assert (a.text, b.text, a.cached, b.cached) == ("once", "once", False, True)
    assert len(s.calls) == 1 and rt.stats["cache"]["hits"] == 1


def test_sampled_requests_are_never_cached() -> None:
    s = Server({GROQ: [(200, ok_body("x"))]})
    rt = router(s)
    rt.complete(req(temperature=0.7))
    rt.complete(req(temperature=0.7))
    assert len(s.calls) == 2 and not cacheable(req(temperature=0.7))


def test_cache_key_covers_every_result_affecting_input() -> None:
    base = cache_key(req())
    assert base == cache_key(req())
    for changed in (
        req("other"),
        req(alias="fast"),
        req(max_tokens=100),
        req(json_output=True),
        req(messages=(Message(role="system", content="s"), Message(role="user", content="hello"))),
    ):
        assert cache_key(changed) != base
    # purpose is accounting only and must not split the cache
    assert cache_key(req(purpose="other")) == base


def test_failures_are_not_cached() -> None:
    s = Server(
        {GROQ: [(500, {}), (200, ok_body("later"))], GEMINI: [(500, {})], OPENR: [(500, {})]}
    )
    clock = Clock()
    rt = router(s, clock)
    with pytest.raises(AllProvidersFailed):
        rt.complete(req())
    clock.t += 120
    assert rt.complete(req()).text == "later"


# ------------------------------------------------------------------------------ mock
def test_mock_alias_never_touches_the_network() -> None:
    s = Server({})
    rt = router(s)
    r = rt.complete(req(alias="mock"))
    assert r.provider == "mock" and s.calls == [] and rt.stats["mock"]["calls"] == 1
    assert rt.complete(req(alias="mock")).cached  # deterministic mock is cached too


def test_chains_cover_every_alias_and_use_distinct_providers() -> None:
    for alias in ("strong", "fast"):
        names = [p.name for p in CHAINS[alias]]
        assert names == ["groq", "gemini", "openrouter"]
        assert all(p.model.endswith(":free") for p in CHAINS[alias] if p.name == "openrouter")
    assert CHAINS["mock"] == ()


# -------------------------------------------------------------------------- limiter
def test_limiter_requests_tokens_refill_and_daily_cap() -> None:
    clock = Clock()
    lim = RateLimiter(rpm=2, tpm=1000, clock=clock, rpd=3)
    assert lim.try_acquire(400) and lim.try_acquire(400)
    assert not lim.try_acquire(1)  # request budget gone
    clock.t += 30  # one request refills
    assert lim.try_acquire(100)
    assert not lim.try_acquire(5000)  # larger than the whole bucket: never possible
    clock.t += 3600
    assert not lim.try_acquire(1)  # daily cap of 3 spent
    tight = RateLimiter(rpm=10, tpm=100, clock=clock)
    assert not tight.try_acquire(101) and tight.try_acquire(100) and not tight.try_acquire(1)
