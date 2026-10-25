"""The file importer (D79): what it reads, what it refuses, and the daily reduction rules.

Every file here is synthetic. The Apple Health shape (a DOCTYPE with an internal DTD, then one
``Record`` per reading, each with a source, unit and timestamps) follows Apple's documented export.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime

import pytest
from vitalmesh_api import healthimport
from vitalmesh_api.app import METRICS
from vitalmesh_api.healthimport import (
    APPLE_TYPES,
    UNITS,
    ImportRefused,
    parse_upload,
    summarise,
)

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<!DOCTYPE HealthData [\n"
    "<!ELEMENT HealthData (ExportDate,Me,(Record)*)>\n"
    "<!ATTLIST HealthData locale CDATA #REQUIRED>\n"
    "]>\n"
    '<HealthData locale="en_US">\n'
    '<ExportDate value="2026-10-26 08:00:00 +0530"/>\n'
    '<Me HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexNotSet"/>\n'
)


def rec(kind: str, unit: str, value: str, start: str, end: str, source: str = "Phone") -> str:
    return (
        f'<Record type="{kind}" sourceName="{source}" unit="{unit}" '
        f'startDate="{start}" endDate="{end}" value="{value}"/>\n'
    )


def apple(*records: str) -> bytes:
    return (HEAD + "".join(records) + "</HealthData>").encode()


def steps(value: str, day: str, source: str = "Phone") -> str:
    return rec(
        "HKQuantityTypeIdentifierStepCount", "count", value,
        f"{day} 08:00:00 +0000", f"{day} 08:30:00 +0000", source,
    )  # fmt: skip


def by(parsed: healthimport.ParsedImport) -> dict[tuple[str, date], float]:
    return {(r.metric, r.day): r.value for r in parsed.readings}


def test_the_importer_units_are_the_ingress_registry_units() -> None:
    assert {k: v.unit for k, v in METRICS.items()} == UNITS
    assert set(APPLE_TYPES.values()) <= set(UNITS)


def test_two_sources_are_not_added_together() -> None:
    xml = apple(
        steps("4000", "2026-10-20"), steps("2000", "2026-10-20"),
        steps("5200", "2026-10-20", source="Watch"),
    )  # fmt: skip
    got = by(parse_upload(xml, "export.xml", NOW))
    assert got[("step_count", date(2026, 10, 20))] == 6000  # the phone's total, not 11,200


def test_weight_takes_the_last_weighing_of_the_day_and_converts_pounds() -> None:
    kind = "HKQuantityTypeIdentifierBodyMass"
    xml = apple(
        rec(kind, "lb", "160", "2026-10-20 07:00:00 +0000", "2026-10-20 07:00:00 +0000"),
        rec(kind, "lb", "154", "2026-10-20 21:00:00 +0000", "2026-10-20 21:00:00 +0000"),
    )
    got = by(parse_upload(xml, "export.xml", NOW))
    assert got[("body_weight", date(2026, 10, 20))] == pytest.approx(69.85, abs=0.01)


def test_resting_heart_rate_is_the_mean_of_the_day() -> None:
    kind = "HKQuantityTypeIdentifierRestingHeartRate"
    xml = apple(
        rec(kind, "count/min", "58", "2026-10-21 06:00:00 +0000", "2026-10-21 06:00:00 +0000"),
        rec(kind, "count/min", "62", "2026-10-21 18:00:00 +0000", "2026-10-21 18:00:00 +0000"),
    )
    assert (
        by(parse_upload(xml, "export.xml", NOW))[("resting_heart_rate", date(2026, 10, 21))] == 60
    )


def test_sleep_counts_only_asleep_stages_on_the_day_it_ended() -> None:
    kind = "HKCategoryTypeIdentifierSleepAnalysis"
    stage = "HKCategoryValueSleepAnalysis"

    def night(suffix: str, start: str, end: str) -> str:
        return rec(kind, "", f"{stage}{suffix}", f"{start} +0000", f"{end} +0000")

    xml = apple(
        night("InBed", "2026-10-21 22:00:00", "2026-10-22 06:30:00"),
        night("AsleepCore", "2026-10-21 23:00:00", "2026-10-22 02:00:00"),
        night("AsleepDeep", "2026-10-22 02:00:00", "2026-10-22 03:30:00"),
        night("Awake", "2026-10-22 03:30:00", "2026-10-22 04:00:00"),
    )
    parsed = parse_upload(xml, "export.xml", NOW)
    assert by(parsed)[("sleep_duration", date(2026, 10, 22))] == 4.5
    assert parsed.skipped["not asleep"] == 2


def test_food_energy_in_kilojoules_becomes_kilocalories() -> None:
    kind = "HKQuantityTypeIdentifierDietaryEnergyConsumed"
    xml = apple(rec(kind, "kJ", "4184", "2026-10-22 12:00:00 +0000", "2026-10-22 12:00:00 +0000"))
    assert by(parse_upload(xml, "export.xml", NOW))[("energy_intake", date(2026, 10, 22))] == 1000


def test_old_records_and_types_we_do_not_use_are_counted_not_imported() -> None:
    xml = apple(
        steps("1000", "2025-01-01"),
        steps("3000", "2026-10-22"),
        rec(
            "HKQuantityTypeIdentifierHeartRate", "count/min", "70",
            "2026-10-22 10:00:00 +0000", "2026-10-22 10:00:00 +0000",
        ),
    )  # fmt: skip
    parsed = parse_upload(xml, "export.xml", NOW)
    assert list(by(parsed)) == [("step_count", date(2026, 10, 22))]
    assert parsed.skipped == {"older than the period": 1, "types we do not use": 1}


def test_a_reading_for_today_is_never_dated_in_the_future_and_keeps_the_local_day() -> None:
    kind = "HKQuantityTypeIdentifierBodyMass"
    xml = apple(
        rec(kind, "kg", "70", "2026-10-26 08:30:00 +0530", "2026-10-26 08:30:00 +0530"),
        rec(kind, "kg", "71", "2026-10-25 07:00:00 +0530", "2026-10-25 07:00:00 +0530"),
    )
    readings = {r.day: r for r in parse_upload(xml, "export.xml", NOW).readings}
    assert readings[date(2026, 10, 26)].measured_at < NOW  # noon would be later than now
    assert readings[date(2026, 10, 25)].measured_at == datetime(2026, 10, 25, 6, 30, tzinfo=UTC)


def test_a_zip_is_opened_and_only_export_xml_is_read() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("apple_health_export/export.xml", apple(steps("3000", "2026-10-22")))
        z.writestr(
            "apple_health_export/export_cda.xml", "<ClinicalDocument>secret</ClinicalDocument>"
        )
    parsed = parse_upload(buf.getvalue(), "export.zip", NOW)
    assert parsed.source == "apple_health" and len(parsed.readings) == 1


def test_a_zip_without_export_xml_is_refused() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("notes.txt", "hello")
    with pytest.raises(ImportRefused, match=r"export\.xml"):
        parse_upload(buf.getvalue(), "export.zip", NOW)


def test_an_entity_expansion_bomb_is_refused_not_expanded() -> None:
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE HealthData [<!ENTITY a "aaaaaaaaaa">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        b'<HealthData><Record type="HKQuantityTypeIdentifierStepCount" value="&b;"/></HealthData>'
    )
    with pytest.raises(ImportRefused, match="XML features"):
        parse_upload(bomb, "export.xml", NOW)


def test_an_external_entity_that_would_read_a_local_file_is_refused() -> None:
    attack = (
        b'<?xml version="1.0"?><!DOCTYPE HealthData [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b'<HealthData><Record type="HKQuantityTypeIdentifierStepCount" value="&x;"/></HealthData>'
    )
    with pytest.raises(ImportRefused, match="XML features"):
        parse_upload(attack, "export.xml", NOW)


def test_a_small_zip_that_expands_hugely_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healthimport, "MAX_XML_BYTES", 5_000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("export.xml", apple(*[steps("10", "2026-10-22")] * 400))
    assert len(buf.getvalue()) < 5_000  # small on the wire, larger once opened
    with pytest.raises(ImportRefused, match="too large once opened"):
        parse_upload(buf.getvalue(), "export.zip", NOW)


def test_too_many_records_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healthimport, "MAX_RECORDS", 3)
    with pytest.raises(ImportRefused, match="too many records"):
        parse_upload(apple(*[steps("10", "2026-10-22")] * 5), "export.xml", NOW)


def test_broken_xml_and_unknown_file_types_are_refused_plainly() -> None:
    with pytest.raises(ImportRefused, match="not a valid Apple Health"):
        parse_upload(b'<?xml version="1.0"?><HealthData><Record', "export.xml", NOW)
    with pytest.raises(ImportRefused, match="not supported"):
        parse_upload(b"\x89PNG\r\n\x1a\n" + bytes(300), "photo.png", NOW)
    with pytest.raises(ImportRefused, match="bigger than 50 MB"):
        parse_upload(bytes(healthimport.MAX_UPLOAD_BYTES + 1), "x.csv", NOW)


# ------------------------------------------------------------------------------------ CSV
def test_a_long_csv_is_read_and_a_wrong_unit_is_skipped_not_converted() -> None:
    data = (
        b"date,metric,value,unit\n"
        b"2026-10-20,body_weight,70.5,kg\n"
        b"2026-10-20,body_weight,155,lb\n"
        b"2026-10-21,step_count,8000,count\n"
        b"2026-10-21,mood,7,score\n"
    )
    parsed = parse_upload(data, "data.csv", NOW)
    assert by(parsed) == {
        ("body_weight", date(2026, 10, 20)): 70.5,
        ("step_count", date(2026, 10, 21)): 8000,
    }
    assert parsed.skipped == {"wrong unit (expected kg)": 1, "metrics we do not use": 1}


def test_a_wide_csv_with_takeout_style_columns_is_read() -> None:
    data = (
        "﻿Date,Step count,Average weight (kg),Move Minutes count,Calories (kcal)\n"
        "2026-10-20,7421,70.2,38,2100\n"
        "2026-10-21,,,,\n"
        "2026-10-22,9100,70.0,52,2300\n"
    ).encode()
    got = by(parse_upload(data, "Daily activity metrics.csv", NOW))
    assert got[("step_count", date(2026, 10, 20))] == 7421
    assert got[("body_weight", date(2026, 10, 22))] == 70.0
    assert got[("active_minutes", date(2026, 10, 22))] == 52
    assert len(got) == 6  # the empty day adds nothing; calories are not a metric we take


def test_a_csv_with_no_known_columns_says_what_it_expects() -> None:
    with pytest.raises(ImportRefused, match="known columns"):
        parse_upload(b"date,mood\n2026-10-20,7\n", "x.csv", NOW)


def test_csv_row_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healthimport, "MAX_CSV_ROWS", 2)
    rows = "".join(f"2026-10-{d},body_weight,70,kg\n" for d in range(10, 15))
    with pytest.raises(ImportRefused, match="too many rows"):
        parse_upload(("date,metric,value,unit\n" + rows).encode(), "x.csv", NOW)


def test_summary_describes_each_metric_for_the_preview() -> None:
    parsed = parse_upload(
        apple(steps("3000", "2026-10-20"), steps("5000", "2026-10-22")), "export.xml", NOW
    )
    assert summarise(parsed.readings) == {
        "step_count": {
            "days": 2, "from": "2026-10-20", "to": "2026-10-22", "min": 3000.0, "max": 5000.0,
            "latest": 5000.0, "unit": "count",
        }
    }  # fmt: skip
