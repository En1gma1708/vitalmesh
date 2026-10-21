from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from vitalmesh_wellness import labs
from vitalmesh_wellness.labs import (
    MARKERS,
    Flag,
    LabError,
    RefRange,
    classify,
    find_marker,
    interpret,
    normalise_unit,
    parse_reference,
)

T = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "low", "high"),
    [
        ("70 - 100", 70, 100),
        ("70-100", 70, 100),
        ("0.5 \u2013 1.2", 0.5, 1.2),
        ("3.5 to 5.0", 3.5, 5.0),
        ("< 50", None, 50),
        ("<=200", None, 200),
        ("Up to 200", None, 200),
        ("> 40", 40, None),
        ("above 40", 40, None),
        ("1,000 - 2,000", 1000, 2000),
    ],
)
def test_simple_ranges_are_parsed(raw: str, low: float | None, high: float | None) -> None:
    r = parse_reference(raw)
    assert (r.parsed, r.low, r.high, r.raw_text) == (True, low, high, raw.strip())


@pytest.mark.parametrize(
    "raw",
    ["", "Desirable: <200", "Normal: 70-100 Prediabetes: 100-125", "5 - 3", "negative", "<", "abc"],
)
def test_anything_else_stays_unparsed_and_unclassified(raw: str) -> None:
    r = parse_reference(raw)
    assert r.parsed is False and r.low is None and r.high is None and r.raw_text == raw.strip()
    assert classify(10, r) is Flag.UNCLASSIFIED


def test_classification_and_boundaries() -> None:
    r = parse_reference("70 - 100")
    assert [classify(v, r) for v in (69.9, 70, 85, 100, 100.1)] == [
        Flag.LOW,
        Flag.NORMAL,
        Flag.NORMAL,
        Flag.NORMAL,
        Flag.HIGH,
    ]
    assert classify(50, parse_reference("< 50")) is Flag.NORMAL  # inclusive boundary (documented)
    assert classify(90, parse_reference("< 50")) is Flag.HIGH
    assert classify(30, parse_reference("> 40")) is Flag.LOW


def test_numeric_result_is_converted_and_flagged_in_the_labs_own_unit() -> None:
    res = interpret("Glucose Fasting", T, value=5.0, unit="mmol/L", reference_text="3.9 - 5.5")
    assert res.marker == "glucose_fasting" and res.loinc == "1558-6"
    assert res.canonical_unit == "mg/dl" and res.canonical_value == pytest.approx(90.08)
    assert res.flag is Flag.NORMAL  # judged in mmol/L against the range as printed
    high = interpret("Glucose Fasting", T, value=7.0, unit="mmol/L", reference_text="3.9 - 5.5")
    assert high.flag is Flag.HIGH


def test_hba1c_conversion_is_affine() -> None:
    res = interpret("HbA1c", T, value=48, unit="mmol/mol", reference_text="< 42")
    assert res.canonical_unit == "%" and res.canonical_value == pytest.approx(6.54, abs=0.01)
    assert res.flag is Flag.HIGH


def test_text_and_unrecognised_and_derived_results_are_kept() -> None:
    t = interpret("RBC Morphology", T, text="Normocytic")
    assert t.text == "Normocytic" and t.flag is Flag.UNCLASSIFIED and t.marker is None
    u = interpret("Ferritin", T, value=80, unit="ng/mL", reference_text="30 - 400")
    assert u.marker is None and u.canonical_value is None and u.flag is Flag.NORMAL
    d = interpret("LDL Cholesterol", T, value=100, unit="mg/dL", derived=True)
    assert d.derived is True and d.flag is Flag.UNCLASSIFIED  # no printed range


@pytest.mark.parametrize(
    "kwargs",
    [
        {"value": 90.0},  # no unit
        {"value": 90.0, "unit": "furlongs"},  # unknown unit for a known marker
        {"value": 9000.0, "unit": "mg/dL"},  # implausible glucose
        {"value": float("nan"), "unit": "mg/dL"},
        {"value": float("inf"), "unit": "mg/dL"},
        {"value": 1.0, "text": "x"},  # both
        {},  # neither
    ],
)
def test_bad_results_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(LabError):
        interpret("Glucose Fasting", T, **kwargs)  # type: ignore[arg-type]


def test_naive_time_and_empty_name_are_rejected() -> None:
    with pytest.raises(LabError):
        interpret("ALT", datetime(2026, 1, 1), value=30, unit="U/L")
    with pytest.raises(LabError):
        interpret("  ", T, value=30, unit="U/L")


def test_unit_spellings_and_names_are_normalised() -> None:
    assert normalise_unit("µmol/L") == normalise_unit("umol/l") == "umol/l"
    assert normalise_unit("IU/L") == "u/l"
    assert normalise_unit("uIU/mL") == "miu/l"
    assert find_marker("  SGPT ") is find_marker("alt")
    assert find_marker("glucose") is None  # ambiguous (random vs fasting): not guessed


def test_dictionary_is_internally_consistent() -> None:
    codes = [m.code for m in MARKERS]
    assert len(codes) == len(set(codes))
    names = [n for m in MARKERS for n in m.names]
    assert len(names) == len(set(names))  # no name maps to two markers
    for m in MARKERS:
        assert m.canonical_unit in m.conversions
        assert m.conversions[m.canonical_unit].to_canonical(5.0) == 5.0
        assert m.plausible_low < m.plausible_high


@given(
    st.sampled_from(MARKERS),
    st.floats(min_value=0.01, max_value=1000, allow_nan=False),
)
def test_conversion_round_trips_for_every_marker_and_unit(marker, value) -> None:  # type: ignore[no-untyped-def]
    for conv in marker.conversions.values():
        back = conv.from_canonical(conv.to_canonical(value))
        assert back == pytest.approx(value, rel=1e-9, abs=1e-9)


@given(
    st.floats(min_value=0, max_value=500, allow_nan=False),
    st.floats(min_value=0, max_value=500, allow_nan=False),
    st.floats(min_value=0, max_value=500, allow_nan=False),
)
def test_classification_is_monotonic_and_consistent(a: float, b: float, v: float) -> None:
    lo, hi = sorted((a, b))
    if lo == hi:
        return
    r = RefRange(raw_text=f"{lo}-{hi}", parsed=True, low=lo, high=hi)
    flag = classify(v, r)
    assert (flag is Flag.LOW) == (v < lo)
    assert (flag is Flag.HIGH) == (v > hi)
    assert (flag is Flag.NORMAL) == (lo <= v <= hi)


# ------------------------------------------------------------------------------ severity
def _lab(value: float, reference: str) -> labs.LabResult:
    return labs.interpret(
        "LDL Cholesterol",
        datetime(2026, 10, 1, tzinfo=UTC),
        value=value,
        unit="mg/dL",
        reference_text=reference,
    )


def test_severity_normal_abnormal_and_marked_on_both_sides() -> None:
    assert labs.severity(_lab(90, "< 100")) is labs.Severity.NORMAL
    assert labs.severity(_lab(128, "< 100")) is labs.Severity.ABNORMAL
    assert labs.severity(_lab(100 * labs.MARKED_FACTOR, "< 100")) is labs.Severity.ABNORMAL
    assert labs.severity(_lab(151, "< 100")) is labs.Severity.MARKED
    assert labs.severity(_lab(40, "40 - 60")) is labs.Severity.NORMAL
    assert labs.severity(_lab(30, "40 - 60")) is labs.Severity.ABNORMAL
    assert labs.severity(_lab(20, "40 - 60")) is labs.Severity.MARKED


def test_severity_is_unknown_not_normal_when_nothing_can_be_judged() -> None:
    assert labs.severity(_lab(128, "Desirable: <100; High: >160")) is labs.Severity.UNKNOWN
    text = labs.interpret("RBC Morphology", datetime(2026, 10, 1, tzinfo=UTC), text="Normocytic")
    assert labs.severity(text) is labs.Severity.UNKNOWN
