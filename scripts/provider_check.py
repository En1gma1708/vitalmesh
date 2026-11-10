"""Is each model provider usable right now? One tiny call per provider (D105).

    uv run python scripts/provider_check.py

Run it before recording the video and before sending anyone a link: the free tiers cap tokens per
day, and when every provider is rate-limited a question is answered from the tools only or, for a
medium-risk question, held for a clinician (a safe design, a bad take). Keys are read from the
gitignored ``.env`` and never printed. Spends a few tokens per provider, synthetic text only.
"""

from __future__ import annotations

import sys
from pathlib import Path

from vitalmesh_llm import LLMRequest, Router, load_keys
from vitalmesh_llm.providers import CHAINS
from vitalmesh_llm.router import _Retryable  # type: ignore[attr-defined]
from vitalmesh_llm.types import Message

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    keys = load_keys(ROOT / ".env")
    router = Router(keys)
    req = LLMRequest(
        alias="fast",
        purpose="provider-check",
        data_class="synthetic",
        messages=(Message(role="user", content="Reply with the word ok."),),
        max_tokens=64,
    )
    seen: set[str] = set()
    usable = 0
    for alias in ("fast", "strong"):
        for spec in CHAINS[alias]:
            tag = f"{spec.name}/{spec.model}"
            if tag in seen:
                continue
            seen.add(tag)
            if not keys.get(spec.key_env):
                print(f"{tag:<44} no key configured")
                continue
            try:
                resp = router._post(spec, req.model_copy(update={"alias": alias}))
                usable += 1
                print(f"{tag:<44} OK ({resp.latency_ms:.0f} ms)")
            except _Retryable as exc:
                wait = f", retry after about {exc.retry_after:.0f} s" if exc.retry_after else ""
                print(f"{tag:<44} LIMITED or failing: {exc.reason}{wait}")
            except Exception as exc:
                print(f"{tag:<44} error: {type(exc).__name__}")
    print(f"\n{usable} of {len(seen)} provider/model pairs usable now.")
    if usable == 0:
        print(
            "Nothing is usable: answers would come from the tools only, and medium-risk "
            "questions would wait for a clinician. Wait and run this again before recording."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
