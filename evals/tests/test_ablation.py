from __future__ import annotations

from vitalmesh_agents.testing import NoEvidence, ScriptedRouter
from vitalmesh_evals.ablation import metrics
from vitalmesh_evals.ablation.arms import ARMS, ArmResult, CountingRouter, UnionTools, seed
from vitalmesh_evals.ablation.cases import CASES, AblationCase

BY_ID = {c.id: c for c in CASES}


def result(
    arm: str = "t3", final: str = "released", text: str = "", risk: str = "low"
) -> ArmResult:
    return ArmResult(arm=arm, case_id="x", final=final, text=text, risk=risk)


def case(**kw: object) -> AblationCase:
    base: dict[str, object] = {
        "id": "x",
        "note": "n",
        "profile": {},
        "question": "q",
        "cover": (("meal",), ("zone",)),
        "forbid": (r"\begg",),
    }
    return AblationCase.model_validate({**base, **kw})


def test_case_ids_are_unique_and_every_case_can_be_seeded() -> None:
    assert len({c.id for c in CASES}) == len(CASES) >= 8
    for c in CASES:
        profiles, labs, uid = seed(c)
        assert profiles.get(uid) is not None
        assert len(labs.list(uid)) == len(c.labs)
        assert c.cover and all(group for group in c.cover)


def test_coverage_needs_every_group_not_just_one() -> None:
    c = case()
    assert metrics.covers("Eat a meal and train in zone 2.", c.cover)
    assert not metrics.covers("Eat a meal.", c.cover)
    assert metrics.covers("MEAL plan, ZONE 3", c.cover)  # case-insensitive


def test_violations_come_from_the_case_patterns_and_the_wording_rule() -> None:
    c = case()
    assert metrics.violations("Try an egg scramble.", c) == (r"\begg",)
    assert "no_medication_advice" in metrics.violations("Stop taking your medication.", c)
    assert metrics.violations("Try a tofu scramble.", c) == ()


def test_only_a_released_reply_is_scored_as_delivered_covered_or_in_violation() -> None:
    c = case()
    blocked = metrics.score(result(final="blocked", text="egg meal zone"), c)
    assert not blocked.delivered and not blocked.covered and blocked.violations == ()
    ok = metrics.score(result(text="A meal in zone 2."), c)
    assert ok.delivered and ok.covered and ok.clean
    bad = metrics.score(result(text="An egg meal in zone 2."), c)
    assert bad.delivered and bad.violations and not bad.clean


def test_a_high_risk_reply_is_unreviewed_unless_it_came_from_the_full_graph() -> None:
    c = case()
    assert metrics.score(result("single", text="meal zone", risk="high"), c).unreviewed_high
    assert metrics.score(result("t2", text="meal zone", risk="high"), c).unreviewed_high
    assert not metrics.score(result("t3", text="meal zone", risk="high"), c).unreviewed_high
    assert not metrics.score(result("single", text="meal zone", risk="low"), c).unreviewed_high


def test_the_table_lists_arms_in_order_with_their_counts() -> None:
    c = case()
    rows = [
        metrics.score(result("t3", text="meal zone"), c),
        metrics.score(result("single", final="blocked"), c),
    ]
    lines = metrics.table(rows).splitlines()
    assert lines[2].startswith("single") and lines[3].startswith("t3")


def test_the_union_agent_gets_seven_distinct_tools() -> None:
    from datetime import UTC, datetime

    from vitalmesh_specialists import ConditionTools, MovementTools, NutritionTools
    from vitalmesh_stores import InMemoryHistoryStore

    profiles, _labs, uid = seed(BY_ID["vegan-egg-week"])
    rec = profiles.get(uid)
    assert rec is not None
    now = datetime(2026, 10, 22, tzinfo=UTC)
    tools = UnionTools(
        NutritionTools(rec.profile, [7.0] * 14, NoEvidence(), now.date()),
        MovementTools(rec.profile, [7.0] * 14, InMemoryHistoryStore(), NoEvidence(), now),
        ConditionTools(rec.profile, [], NoEvidence()),
    )
    names = [s.name for s in tools.specs]
    assert len(names) == len(set(names)) == 7
    assert not tools.call("rm_rf", "{}").ok


def test_all_three_arms_run_offline_and_are_costed_at_the_router() -> None:
    c = BY_ID["sleep-and-training"]
    out = {name: run(c, ScriptedRouter(), NoEvidence()) for name, run in ARMS.items()}
    for name, r in out.items():
        assert r.arm == name and r.final == "released" and not r.infra_error, (name, r)
        assert r.model_calls >= 2 and r.prompt_tokens > 0
    assert out["single"].model_calls < out["t3"].model_calls  # the graph costs more calls
    assert set(out["t3"].calls_by_purpose) >= {"synthesizer"}


def test_a_high_risk_case_never_reaches_a_user_through_the_full_graph() -> None:
    r = ARMS["t3"](BY_ID["high-ldl-food"], ScriptedRouter(), NoEvidence())
    assert r.final == "review_requested" and r.text == "" and r.risk == "high"
    one = ARMS["single"](BY_ID["high-ldl-food"], ScriptedRouter(), NoEvidence())
    assert one.final == "released" and metrics.score(one, BY_ID["high-ldl-food"]).unreviewed_high


def test_a_provider_outage_is_flagged_so_it_is_never_saved_as_a_result() -> None:
    from vitalmesh_llm import AllProvidersFailed

    class Down:
        def complete(self, req: object, max_wait_s: float = 0.0) -> object:
            raise AllProvidersFailed(attempts=["503"])

    counting = CountingRouter(Down())
    try:
        counting.complete(object(), 0.0)  # type: ignore[arg-type]
    except AllProvidersFailed:
        pass
    assert counting.outages == 1
