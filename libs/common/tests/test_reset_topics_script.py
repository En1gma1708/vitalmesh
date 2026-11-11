"""scripts/reset_topics.py on a brand-new broker (found on the first real Azure deploy, D99).

The client library raises ValueError for an empty delete list, and a fresh broker has none of the
contract topics yet. The script must skip the delete and just create them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "reset_topics.py"


class _Future:
    def result(self) -> None:
        return None


class _FakeAdmin:
    """Stands in for the Kafka admin client; refuses an empty delete like the real one."""

    def __init__(self, present: list[str]) -> None:
        self.present = list(present)
        self.deleted: list[str] = []

    def list_topics(self, timeout: float) -> Any:
        return SimpleNamespace(topics={n: None for n in self.present})

    def delete_topics(self, topics: list[str], operation_timeout: float) -> dict[str, _Future]:
        if not topics:
            raise ValueError("Expected non-empty list of topic strings")
        self.deleted.extend(topics)
        self.present = [n for n in self.present if n not in topics]
        return {t: _Future() for t in topics}


def _load(monkeypatch: pytest.MonkeyPatch, admin: _FakeAdmin) -> Any:
    spec = importlib.util.spec_from_file_location("reset_topics_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "AdminClient", lambda conf: admin)
    monkeypatch.setattr(mod, "ensure_topics", lambda settings: ["created-a", "created-b"])
    monkeypatch.setattr(
        mod.KafkaSettings,
        "from_env",
        classmethod(lambda cls: SimpleNamespace(bootstrap_servers="127.0.0.1:1")),
    )
    return mod


def test_fresh_broker_with_no_topics_does_not_crash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _FakeAdmin(present=[])
    mod = _load(monkeypatch, admin)
    mod.main()
    assert admin.deleted == []
    assert "deleted 0 topics, created 2" in capsys.readouterr().out


def test_existing_topics_are_deleted_then_recreated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from vitalmesh_common.retry import RetryPolicy
    from vitalmesh_common.topics import all_topic_specs

    names = [s.name for s in all_topic_specs(RetryPolicy().max_retries)]
    admin = _FakeAdmin(present=[names[0], "unrelated-topic"])
    mod = _load(monkeypatch, admin)
    mod.main()
    assert admin.deleted == [names[0]]  # only contract topics, never the unrelated one
    assert "deleted 1 topics" in capsys.readouterr().out


def test_refuses_a_broker_that_is_not_local(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load(monkeypatch, _FakeAdmin(present=[]))
    monkeypatch.setattr(
        mod.KafkaSettings,
        "from_env",
        classmethod(lambda cls: SimpleNamespace(bootstrap_servers="10.0.0.5:9092")),
    )
    with pytest.raises(SystemExit):
        mod.main()
