from __future__ import annotations

from vitalmesh_agents.cohort import (
    CITED,
    COMPUTED,
    ClaimView,
    archetype_of,
    build_template,
    cache_key,
    cohort_eligible,
    normalise_question,
    personalise,
)
from vitalmesh_agents.triage import Signals
from vitalmesh_wellness.synthetic import synthetic_user


def computed(metric: str, value: float, unit: str, text: str) -> ClaimView:
    return ClaimView(
        text=text, provenance=COMPUTED, source="calc@1", metric=metric, value=value, unit=unit
    )


CITE = ClaimView(
    text="Adults need at least 150 minutes of moderate activity a week.",
    provenance=CITED,
    source="who-2020 p.1",
)


def user_a() -> tuple[str, list[ClaimView]]:
    claims = [
        computed("energy_target", 2168, "kcal", "Aim for about 2,168 kcal a day."),
        computed("sleep_goal", 9.0, "h", "Aim for 9.0 hours of sleep."),
        CITE,
    ]
    text = "Aim for about 2,168 kcal a day and 9.0 hours of sleep; adults need 150 minutes a week."
    return text, claims


def user_b() -> list[ClaimView]:
    return [
        computed("energy_target", 1850, "kcal", "Aim for about 1,850 kcal a day."),
        computed("sleep_goal", 8.0, "h", "Aim for 8.0 hours of sleep."),
    ]


def test_template_is_personalised_for_another_user() -> None:
    text, claims = user_a()
    template = build_template(text, claims)
    assert template is not None
    out = personalise(template, user_b())
    assert out is not None
    new_text, new_claims = out
    assert new_text == (
        "Aim for about 1,850 kcal a day and 8.0 hours of sleep; adults need 150 minutes a week."
    )
    assert [c.provenance for c in new_claims] == [COMPUTED, COMPUTED, CITED]
    assert new_claims[0].value == 1850 and new_claims[2].text == CITE.text


def test_round_trip_reproduces_the_original_text() -> None:
    text, claims = user_a()
    template = build_template(text, claims)
    assert template is not None
    assert personalise(template, claims) == (text, claims)


def test_a_number_without_a_source_is_not_cacheable() -> None:
    text, claims = user_a()
    assert build_template(text + " Try 3 sets.", claims) is None


def test_inferred_claims_are_not_cacheable() -> None:
    text, claims = user_a()
    inferred = ClaimView(text="x", provenance="inferred", source="basis")
    assert build_template(text, [*claims, inferred]) is None


def test_ambiguous_number_is_not_cacheable() -> None:
    claims = [computed("a", 9.0, "h", "A is 9.0."), computed("b", 9.0, "h", "B is 9.0.")]
    assert build_template("A and B are both 9.0.", claims) is None


def test_missing_fresh_claim_is_a_miss() -> None:
    text, claims = user_a()
    template = build_template(text, claims)
    assert template is not None
    assert personalise(template, user_b()[:1]) is None


def test_question_normalisation_ignores_case_spacing_and_punctuation() -> None:
    assert normalise_question("  How MUCH   should I sleep?! ") == "how much should i sleep"


def test_key_covers_every_input() -> None:
    profile, _ = synthetic_user(3, 1)
    arch = archetype_of(profile)
    base = cache_key("How much should I sleep?", arch, ["movement"], "3+t3-1")
    assert base == cache_key("how much should i sleep", arch, ["movement"], "3+t3-1")
    assert base != cache_key("how much should i sleep", arch, ["movement"], "3+t3-2")
    assert base != cache_key("how much should i sleep", arch, ["nutrition"], "3+t3-1")
    other = archetype_of(profile.model_copy(update={"diet": "vegan"}))
    assert base != cache_key("how much should i sleep", other, ["movement"], "3+t3-1")


def test_only_users_without_constraints_are_eligible() -> None:
    assert cohort_eligible(Signals())
    assert not cohort_eligible(Signals(conditions=frozenset({"asthma"})))
    assert not cohort_eligible(Signals(abnormal_labs=("ldl",)))
