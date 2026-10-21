"""Lab results, pure and deterministic (D42, D45, D51).

What this module guarantees:

* the reference range is kept as the lab printed it (``raw_text``) and parsed only when it is a
  simple interval or one-sided limit; anything else is ``parsed=False`` and the result is
  classified ``UNCLASSIFIED`` rather than guessed;
* a result is classified against the range **in the lab's own unit**, so a unit conversion can
  never move a value across a range boundary;
* unit conversion is explicit and checked (factor plus offset, with an exact inverse), unknown
  units are rejected rather than assumed, and implausible values are rejected, never clamped;
* a marker that is not in the dictionary is still kept (name, value, unit, range) but gets no
  canonical value and no coaching.

LOINC codes below were written from memory and must be checked against loinc.org before any
demo; ``loinc`` is informational and never used for logic.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

LABS_VERSION = "labs-1"


class LabError(ValueError):
    """The result cannot be accepted (unknown unit, implausible value, bad input)."""


class Flag(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    UNCLASSIFIED = "unclassified"


class Conversion(BaseModel):
    """canonical = value * factor + offset (factor must be positive: order is preserved)."""

    model_config = ConfigDict(frozen=True)

    factor: float = Field(gt=0)
    offset: float = 0.0

    def to_canonical(self, value: float) -> float:
        return value * self.factor + self.offset

    def from_canonical(self, value: float) -> float:
        return (value - self.offset) / self.factor


class Marker(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str  # internal stable key
    name: str
    loinc: str | None
    canonical_unit: str
    names: tuple[str, ...]  # lower-case names as labs print them
    conversions: dict[str, Conversion]  # normalised unit -> conversion to canonical
    plausible_low: float
    plausible_high: float  # in canonical units


def _same(*units: str) -> dict[str, Conversion]:
    return {u: Conversion(factor=1.0) for u in units}


MARKERS: tuple[Marker, ...] = (
    Marker(
        code="glucose_fasting",
        name="Fasting glucose",
        loinc="1558-6",
        canonical_unit="mg/dl",
        names=("glucose fasting", "fasting glucose", "fasting blood sugar", "fbs"),
        conversions={"mg/dl": Conversion(factor=1.0), "mmol/l": Conversion(factor=18.016)},
        plausible_low=20,
        plausible_high=800,
    ),
    Marker(
        code="hba1c",
        name="HbA1c",
        loinc="4548-4",
        canonical_unit="%",
        names=("hba1c", "glycosylated haemoglobin", "glycated hemoglobin", "glycohemoglobin"),
        # NGSP % = 0.09148 * IFCC mmol/mol + 2.152 (affine, not proportional)
        conversions={
            "%": Conversion(factor=1.0),
            "mmol/mol": Conversion(factor=0.09148, offset=2.152),
        },
        plausible_low=3,
        plausible_high=20,
    ),
    Marker(
        code="cholesterol_total",
        name="Total cholesterol",
        loinc="2093-3",
        canonical_unit="mg/dl",
        names=("cholesterol total", "total cholesterol", "cholesterol"),
        conversions={"mg/dl": Conversion(factor=1.0), "mmol/l": Conversion(factor=38.67)},
        plausible_low=50,
        plausible_high=700,
    ),
    Marker(
        code="hdl_cholesterol",
        name="HDL cholesterol",
        loinc="2085-9",
        canonical_unit="mg/dl",
        names=("hdl cholesterol", "hdl", "cholesterol hdl", "hdl-c"),
        conversions={"mg/dl": Conversion(factor=1.0), "mmol/l": Conversion(factor=38.67)},
        plausible_low=5,
        plausible_high=200,
    ),
    Marker(
        code="ldl_cholesterol",
        name="LDL cholesterol",
        loinc="13457-7",
        canonical_unit="mg/dl",
        names=("ldl cholesterol", "ldl", "cholesterol ldl", "ldl-c", "ldl cholesterol calculated"),
        conversions={"mg/dl": Conversion(factor=1.0), "mmol/l": Conversion(factor=38.67)},
        plausible_low=5,
        plausible_high=500,
    ),
    Marker(
        code="triglycerides",
        name="Triglycerides",
        loinc="2571-8",
        canonical_unit="mg/dl",
        names=("triglycerides", "triglyceride", "tg"),
        conversions={"mg/dl": Conversion(factor=1.0), "mmol/l": Conversion(factor=88.57)},
        plausible_low=10,
        plausible_high=3000,
    ),
    Marker(
        code="creatinine",
        name="Creatinine",
        loinc="2160-0",
        canonical_unit="mg/dl",
        names=("creatinine", "serum creatinine"),
        conversions={"mg/dl": Conversion(factor=1.0), "umol/l": Conversion(factor=1 / 88.4)},
        plausible_low=0.1,
        plausible_high=20,
    ),
    Marker(
        code="alt",
        name="ALT",
        loinc="1742-6",
        canonical_unit="u/l",
        names=("alt", "sgpt", "alanine aminotransferase", "alanine transaminase"),
        conversions=_same("u/l"),
        plausible_low=1,
        plausible_high=5000,
    ),
    Marker(
        code="ast",
        name="AST",
        loinc="1920-8",
        canonical_unit="u/l",
        names=("ast", "sgot", "aspartate aminotransferase", "aspartate transaminase"),
        conversions=_same("u/l"),
        plausible_low=1,
        plausible_high=5000,
    ),
    Marker(
        code="tsh",
        name="TSH",
        loinc="3016-3",
        canonical_unit="miu/l",
        names=("tsh", "thyroid stimulating hormone"),
        conversions=_same("miu/l"),
        plausible_low=0.001,
        plausible_high=500,
    ),
    Marker(
        code="hemoglobin",
        name="Hemoglobin",
        loinc="718-7",
        canonical_unit="g/dl",
        names=("hemoglobin", "haemoglobin", "hb", "hgb"),
        conversions={"g/dl": Conversion(factor=1.0), "g/l": Conversion(factor=0.1)},
        plausible_low=2,
        plausible_high=25,
    ),
)

_BY_NAME: dict[str, Marker] = {n: m for m in MARKERS for n in m.names}
_BY_CODE: dict[str, Marker] = {m.code: m for m in MARKERS}

# Spellings of the same unit, after lower-casing and unifying the micro sign.
_UNIT_ALIASES = {
    "iu/l": "u/l",
    "u/l": "u/l",
    "uiu/ml": "miu/l",  # 1 uIU/mL == 1 mIU/L
    "miu/l": "miu/l",
    "mg/dl": "mg/dl",
    "mg/100ml": "mg/dl",
    "mmol/l": "mmol/l",
    "umol/l": "umol/l",
    "g/dl": "g/dl",
    "g/l": "g/l",
    "%": "%",
    "mmol/mol": "mmol/mol",
}


def normalise_unit(unit: str) -> str:
    u = unit.strip().lower().replace("µ", "u").replace("μ", "u").replace(" ", "")
    return _UNIT_ALIASES.get(u, u)


def find_marker(name: str) -> Marker | None:
    key = re.sub(r"\s+", " ", name.strip().lower())
    return _BY_NAME.get(key)


def marker_by_code(code: str) -> Marker | None:
    return _BY_CODE.get(code)


# ----------------------------------------------------------------------------- ranges
class RefRange(BaseModel):
    model_config = ConfigDict(frozen=True)

    raw_text: str
    parsed: bool = False
    low: float | None = None  # None with parsed=True means "no lower limit"
    high: float | None = None


_NUM = r"(\d+(?:\.\d+)?)"
_INTERVAL = re.compile(rf"^{_NUM}\s*(?:-|\u2013|\u2014|to)\s*{_NUM}$")
_UPPER = re.compile(rf"^(?:<=?|up to|below|upto)\s*{_NUM}$")
_LOWER = re.compile(rf"^(?:>=?|above|more than|greater than)\s*{_NUM}$")


def parse_reference(raw: str) -> RefRange:
    """Parse only what is unambiguous. Never guess: unparsed ranges stay unclassified."""
    text = raw.strip()
    cleaned = text.lower().replace(",", "")
    if m := _INTERVAL.match(cleaned):
        low, high = float(m.group(1)), float(m.group(2))
        if low < high:
            return RefRange(raw_text=text, parsed=True, low=low, high=high)
    elif m := _UPPER.match(cleaned):
        return RefRange(raw_text=text, parsed=True, high=float(m.group(1)))
    elif m := _LOWER.match(cleaned):
        return RefRange(raw_text=text, parsed=True, low=float(m.group(1)))
    return RefRange(raw_text=text)


def classify(value: float, ref: RefRange) -> Flag:
    """Boundary values count as normal (the printed limits are inclusive here)."""
    if not ref.parsed or (ref.low is None and ref.high is None):
        return Flag.UNCLASSIFIED
    if ref.low is not None and value < ref.low:
        return Flag.LOW
    if ref.high is not None and value > ref.high:
        return Flag.HIGH
    return Flag.NORMAL


# ---------------------------------------------------------------------------- results
class LabResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    marker: str | None  # internal code when the marker is in the dictionary
    name: str  # as the lab printed it
    loinc: str | None = None
    value: float | None = None  # in the unit the lab used
    unit: str | None = None
    text: str | None = None  # qualitative result
    canonical_value: float | None = None
    canonical_unit: str | None = None
    reference: RefRange
    flag: Flag
    collected_at: datetime
    derived: bool = False  # "Method: Calculated": not an independent measurement


def interpret(
    name: str,
    collected_at: datetime,
    *,
    value: float | None = None,
    unit: str | None = None,
    text: str | None = None,
    reference_text: str = "",
    derived: bool = False,
) -> LabResult:
    if (value is None) == (text is None):
        raise LabError("a result is either numeric or text, not both and not neither")
    if collected_at.tzinfo is None:
        raise LabError("collected_at must include a timezone")
    name = name.strip()
    if not name:
        raise LabError("a result needs a marker name")
    ref = parse_reference(reference_text) if reference_text.strip() else RefRange(raw_text="")
    marker = find_marker(name)

    if text is not None:
        return LabResult(
            marker=marker.code if marker else None,
            name=name,
            loinc=marker.loinc if marker else None,
            text=text.strip(),
            reference=ref,
            flag=Flag.UNCLASSIFIED,
            collected_at=collected_at,
            derived=derived,
        )

    assert value is not None
    if not math.isfinite(value):
        raise LabError("value must be a finite number")
    if not unit or not unit.strip():
        raise LabError("a numeric result needs a unit")
    unit_norm = normalise_unit(unit)
    flag = classify(value, ref)

    if marker is None:  # kept, but no canonical value and no coaching
        return LabResult(
            marker=None,
            name=name,
            value=value,
            unit=unit.strip(),
            reference=ref,
            flag=flag,
            collected_at=collected_at,
            derived=derived,
        )
    conv = marker.conversions.get(unit_norm)
    if conv is None:
        raise LabError(f"unit {unit!r} is not a known unit for {marker.name}")
    canonical = conv.to_canonical(value)
    if not marker.plausible_low <= canonical <= marker.plausible_high:
        raise LabError(
            f"{marker.name} of {value} {unit} is outside the plausible range; check for a typo"
        )
    return LabResult(
        marker=marker.code,
        name=name,
        loinc=marker.loinc,
        value=value,
        unit=unit.strip(),
        canonical_value=canonical,
        canonical_unit=marker.canonical_unit,
        reference=ref,
        flag=flag,
        collected_at=collected_at,
        derived=derived,
    )


# --------------------------------------------------------------------------------- severity
# Illustrative policy, NOT a clinical threshold and not cited (same status as D37): a value more
# than half as far again beyond a printed limit is "marked" and routes to a clinician.
MARKED_FACTOR = 1.5


class Severity(StrEnum):
    NORMAL = "normal"
    ABNORMAL = "abnormal"  # outside the lab's range
    MARKED = "marked"  # far outside it: a human should look
    UNKNOWN = "unknown"  # qualitative, unparsed range or no number: never treated as normal


def severity(result: LabResult) -> Severity:
    """How far a numeric result is outside the range the lab printed, in the lab's own unit."""
    if result.value is None or result.flag is Flag.UNCLASSIFIED:
        return Severity.UNKNOWN
    if result.flag is Flag.NORMAL:
        return Severity.NORMAL
    ref = result.reference
    if result.flag is Flag.HIGH and ref.high is not None and ref.high > 0:
        return Severity.MARKED if result.value > ref.high * MARKED_FACTOR else Severity.ABNORMAL
    if result.flag is Flag.LOW and ref.low is not None and ref.low > 0:
        return Severity.MARKED if result.value < ref.low / MARKED_FACTOR else Severity.ABNORMAL
    return Severity.ABNORMAL
