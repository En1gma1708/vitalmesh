"""Import health data from a file the person already has (D79).

Why a file: Google Fit's API is closed to new apps and Health Connect lives only on an Android
phone, so the dependable route today is the export a person can download from their own health
app. Three inputs are understood:

* an Apple Health export (``export.zip`` or its ``export.xml``),
* a long CSV (``date,metric,value[,unit]``),
* a wide CSV with one column per metric, including the daily summary that Google Takeout writes for
  Fit (column names taken from third-party descriptions, never checked against an official
  specification: best effort).

The parser reduces many sensor readings to one value per metric per day and returns them for the
person to look at BEFORE anything is stored. It never writes. Out-of-range numbers are not clamped
here; the API validates every reading with the same rules as a hand-entered measurement.

Untrusted input, so: the XML parser refuses entity expansion and external references
(``defusedxml``), the decompressed size, the number of records and the number of CSV rows are
capped, and a file is read as a stream, never extracted to disk.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import IO

from defusedxml import ElementTree as SafeET

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_XML_BYTES = 400 * 1024 * 1024  # after decompression
MAX_RECORDS = 3_000_000
MAX_CSV_ROWS = 200_000
DEFAULT_DAYS = 90

# Must equal the ingress registry's units (a test compares the two).
UNITS = {
    "body_weight": "kg",
    "resting_heart_rate": "bpm",
    "sleep_duration": "h",
    "step_count": "count",
    "active_minutes": "min",
    "energy_intake": "kcal",
}
SUMMED = ("step_count", "active_minutes", "sleep_duration", "energy_intake")
ASLEEP = {
    "HKCategoryValueSleepAnalysisAsleep",
    "HKCategoryValueSleepAnalysisAsleepUnspecified",
    "HKCategoryValueSleepAnalysisAsleepCore",
    "HKCategoryValueSleepAnalysisAsleepDeep",
    "HKCategoryValueSleepAnalysisAsleepREM",
}
APPLE_TYPES = {
    "HKQuantityTypeIdentifierBodyMass": "body_weight",
    "HKQuantityTypeIdentifierRestingHeartRate": "resting_heart_rate",
    "HKQuantityTypeIdentifierStepCount": "step_count",
    "HKQuantityTypeIdentifierAppleExerciseTime": "active_minutes",
    "HKCategoryTypeIdentifierSleepAnalysis": "sleep_duration",
    "HKQuantityTypeIdentifierDietaryEnergyConsumed": "energy_intake",
}
WEIGHT_TO_KG = {"kg": 1.0, "lb": 0.45359237, "g": 0.001}
ENERGY_TO_KCAL = {"kcal": 1.0, "Cal": 1.0, "kJ": 1 / 4.184}
# header -> metric for the wide CSV (lower case); our own names plus the Takeout daily summary
COLUMNS = {
    **{name: name for name in UNITS},
    "step count": "step_count",
    "average weight (kg)": "body_weight",
    "move minutes count": "active_minutes",
}


class ImportRefused(ValueError):
    """The file cannot be imported; the message is plain language for the person."""


@dataclass(frozen=True)
class Reading:
    metric: str
    day: date
    value: float
    unit: str
    measured_at: datetime


@dataclass
class ParsedImport:
    source: str
    readings: list[Reading] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str, n: int = 1) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + n


# ----------------------------------------------------------------------------- upload
class _Capped:
    """A read-only stream that stops with a plain error once too many bytes came out of it, so a
    small compressed file cannot expand into memory or time without limit."""

    def __init__(self, raw: IO[bytes], limit: int) -> None:
        self._raw, self._left = raw, limit

    def read(self, n: int = -1) -> bytes:
        data = self._raw.read(n)
        self._left -= len(data)
        if self._left < 0:
            raise ImportRefused("That file is too large once opened. Export a shorter period.")
        return data


def parse_upload(
    data: bytes, filename: str, now: datetime, days: int = DEFAULT_DAYS
) -> ParsedImport:
    """Pick the right reader by what the bytes are, not by what the name claims."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImportRefused("That file is bigger than 50 MB. Export a shorter period.")
    if data[:2] == b"PK":
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise ImportRefused("That zip file could not be opened.") from None
        names = [n for n in archive.namelist() if n.rsplit("/", 1)[-1] == "export.xml"]
        if not names:
            raise ImportRefused("There is no export.xml in that zip. Use the Apple Health export.")
        with archive.open(names[0]) as member:
            return parse_apple_health(_Capped(member, MAX_XML_BYTES), now, days)
    head = data[:2048].lstrip()
    if head.startswith(b"<?xml") or head.startswith(b"<HealthData"):
        return parse_apple_health(_Capped(io.BytesIO(data), MAX_XML_BYTES), now, days)
    if filename.lower().endswith((".csv", ".txt")) or b"," in head[:400]:
        return parse_csv(data, now, days)
    raise ImportRefused("That file type is not supported. Use an Apple Health export or a CSV.")


# --------------------------------------------------------------------------- Apple Health
def _parse_stamp(text: str) -> datetime:
    # "2024-03-01 07:12:00 +0530" -> ISO 8601
    return datetime.fromisoformat(f"{text[:10]}T{text[11:19]}{text[20:].strip()}")


def _local_noon(day: date, zone: tzinfo | None, now: datetime) -> datetime:
    at = datetime.combine(day, time(12, 0), tzinfo=zone or UTC)
    return min(at, now - timedelta(minutes=1))  # never in the future


def parse_apple_health(stream: IO[bytes] | _Capped, now: datetime, days: int) -> ParsedImport:
    out = ParsedImport("apple_health")
    first_day = (now - timedelta(days=days)).date()
    last_day = now.date() + timedelta(days=1)
    totals: dict[tuple[str, date], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    weight: dict[date, tuple[datetime, float]] = {}
    hr_sum: dict[date, list[float]] = defaultdict(lambda: [0.0, 0.0])
    zones: dict[date, tzinfo | None] = {}
    examined = 0
    try:
        for _event, el in SafeET.iterparse(stream, events=("end",)):
            if el.tag != "Record":
                el.clear()
                continue
            examined += 1
            if examined > MAX_RECORDS:
                raise ImportRefused("That export has too many records. Export a shorter period.")
            attrs = el.attrib
            metric = APPLE_TYPES.get(attrs.get("type", ""))
            el.clear()
            if metric is None:
                out.skip("types we do not use")
                continue
            try:
                end = _parse_stamp(attrs["endDate"])
                day = end.date()
                if not first_day <= day <= last_day:
                    out.skip("older than the period")
                    continue
                zones[day] = end.tzinfo
                source = attrs.get("sourceName", "?")
                unit = attrs.get("unit", "")
                if metric == "sleep_duration":
                    if attrs.get("value") not in ASLEEP:
                        out.skip("not asleep")
                        continue
                    hours = (end - _parse_stamp(attrs["startDate"])).total_seconds() / 3600
                    totals[(metric, day)][source] += hours
                    continue
                value = float(attrs["value"])
                if metric == "body_weight":
                    if unit not in WEIGHT_TO_KG:
                        out.skip("unknown weight unit")
                        continue
                    kg = value * WEIGHT_TO_KG[unit]
                    if day not in weight or end >= weight[day][0]:
                        weight[day] = (end, kg)  # the last weighing of the day
                elif metric == "resting_heart_rate":
                    hr_sum[day][0] += value
                    hr_sum[day][1] += 1
                elif metric == "energy_intake":
                    if unit not in ENERGY_TO_KCAL:
                        out.skip("unknown energy unit")
                        continue
                    totals[(metric, day)][source] += value * ENERGY_TO_KCAL[unit]
                else:
                    totals[(metric, day)][source] += value
            except (KeyError, ValueError):
                out.skip("unreadable record")
    except ImportRefused:
        raise
    except SafeET.ParseError:
        raise ImportRefused("That file is not a valid Apple Health export.") from None
    except Exception as exc:  # defusedxml's refusals (entities, external references)
        if type(exc).__module__.startswith("defusedxml"):
            raise ImportRefused("That file uses XML features we do not accept.") from None
        raise

    def add(metric: str, day: date, value: float) -> None:
        shown = round(value) if metric == "step_count" else round(value, 2)
        out.readings.append(
            Reading(metric, day, float(shown), UNITS[metric], _local_noon(day, zones.get(day), now))
        )

    for day, (_at, kg) in weight.items():
        add("body_weight", day, kg)
    for day, (total, n) in hr_sum.items():
        add("resting_heart_rate", day, total / n)
    for (metric, day), by_source in totals.items():
        # an iPhone and a Watch both count steps: adding sources would count them twice, so use
        # the one source with the highest total for that day
        add(metric, day, max(by_source.values()))
    out.readings.sort(key=lambda r: (r.metric, r.day))
    return out


# ------------------------------------------------------------------------------------ CSV
def _csv_day(text: str) -> date:
    return date.fromisoformat(text.strip()[:10])


def parse_csv(data: bytes, now: datetime, days: int) -> ParsedImport:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportRefused("That CSV is not UTF-8 text.") from None
    reader = csv.reader(io.StringIO(text))
    header = [h.strip().lower() for h in next(reader, [])]
    out = ParsedImport("csv")
    first_day = (now - timedelta(days=days)).date()
    pairs: list[tuple[date, str, float, str | None]] = []
    long_form = "metric" in header and "value" in header and "date" in header
    wide = {i: COLUMNS[h] for i, h in enumerate(header) if h in COLUMNS and h != "date"}
    if not long_form and not wide:
        raise ImportRefused(
            "I could not find any known columns. Use date,metric,value[,unit] or one column per "
            "metric (" + ", ".join(UNITS) + ")."
        )
    for n, row in enumerate(reader, start=1):
        if n > MAX_CSV_ROWS:
            raise ImportRefused("That CSV has too many rows. Send a shorter period.")
        if not any(cell.strip() for cell in row):
            continue
        try:
            day = _csv_day(row[header.index("date")])
            if long_form:
                metric = row[header.index("metric")].strip().lower()
                metric = COLUMNS.get(metric, metric)
                unit = row[header.index("unit")].strip() if "unit" in header else None
                pairs.append((day, metric, float(row[header.index("value")]), unit or None))
            else:
                for i, metric in wide.items():
                    if i < len(row) and row[i].strip():
                        pairs.append((day, metric, float(row[i]), None))
        except (ValueError, IndexError):
            out.skip("unreadable row")
    for day, metric, value, unit in pairs:
        if metric not in UNITS:
            out.skip("metrics we do not use")
        elif unit is not None and unit != UNITS[metric]:
            out.skip(f"wrong unit (expected {UNITS[metric]})")
        elif not first_day <= day <= now.date():
            out.skip("older than the period")
        else:
            at = min(datetime.combine(day, time(12, 0), tzinfo=UTC), now - timedelta(minutes=1))
            out.readings.append(Reading(metric, day, value, UNITS[metric], at))
    out.readings.sort(key=lambda r: (r.metric, r.day))
    return out


def summarise(readings: Iterable[Reading]) -> dict[str, dict[str, object]]:
    """Per metric: how many days, the period, the range, and the latest value (for the preview)."""
    by: dict[str, list[Reading]] = defaultdict(list)
    for r in readings:
        by[r.metric].append(r)
    return {
        metric: {
            "days": len(rs),
            "from": min(r.day for r in rs).isoformat(),
            "to": max(r.day for r in rs).isoformat(),
            "min": min(r.value for r in rs),
            "max": max(r.value for r in rs),
            "latest": max(rs, key=lambda r: r.day).value,
            "unit": rs[0].unit,
        }
        for metric, rs in sorted(by.items())
    }
