"""Provider chains. All three speak the OpenAI-compatible chat-completions protocol.

Limits are the free-tier figures found on 2026-10-20 (D55). Groq's come from its developer-plan
table for gpt-oss and qwen models; Gemini's are NOT published on its docs page, so the values
below are deliberately conservative placeholders and must be replaced with the figures AI Studio
shows for the account. OpenRouter ``:free`` models: 20 requests/min and 50/day without credits.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from vitalmesh_llm.types import Alias


class ProviderSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    base_url: str
    key_env: str
    model: str
    rpm: int
    tpm: int
    rpd: int | None = None


GROQ_STRONG = ProviderSpec(
    name="groq",
    base_url="https://api.groq.com/openai/v1",
    key_env="GROQ_API_KEY",
    model="openai/gpt-oss-120b",
    rpm=30,
    tpm=8_000,
    rpd=1_000,
)
GROQ_FAST = GROQ_STRONG.model_copy(update={"model": "openai/gpt-oss-20b"})
GEMINI_STRONG = ProviderSpec(
    name="gemini",
    base_url="https://generativelanguage.googleapis.com/v1beta/openai",
    key_env="GEMINI_API_KEY",
    model="gemini-3.8-flash",
    rpm=5,
    tpm=100_000,
    rpd=100,
)
GEMINI_FAST = GEMINI_STRONG.model_copy(update={"model": "gemini-3.5-flash-lite", "rpm": 10})
OPENROUTER_STRONG = ProviderSpec(
    name="openrouter",
    base_url="https://openrouter.ai/api/v1",
    key_env="OPENROUTER_API_KEY",
    model="google/gemma-4-31b-it:free",
    rpm=20,
    tpm=100_000,
    rpd=50,
)
OPENROUTER_FAST = OPENROUTER_STRONG.model_copy(update={"model": "google/gemma-4-26b-a4b-it:free"})

CHAINS: dict[Alias, tuple[ProviderSpec, ...]] = {
    "strong": (GROQ_STRONG, GEMINI_STRONG, OPENROUTER_STRONG),
    "fast": (GROQ_FAST, GEMINI_FAST, OPENROUTER_FAST),
    "mock": (),
}


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_keys(env_file: Path | None = None) -> Mapping[str, str]:
    """Keys from the process environment first, then a local ``.env``. Never logged."""
    file_vals = read_env_file(env_file or Path(".env"))
    names = {s.key_env for chain in CHAINS.values() for s in chain}
    return {n: os.environ.get(n) or file_vals.get(n, "") for n in names}
