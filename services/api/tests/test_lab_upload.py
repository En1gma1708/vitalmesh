"""Lab report upload (D86): synthetic reports only, nothing stored until the person confirms."""

from __future__ import annotations

import io
import shutil
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_security import World, csrf
from vitalmesh_api import labextract
from vitalmesh_api.labextract import ExtractRefused, parse_lines
from vitalmesh_common.testing import PlainSerde

REPORT = [
    "Synthetic Lab Services - Test Report",
    "Sample collected: 03/11/2026",
    "Test Result Unit Reference",
    "LDL Cholesterol 190 mg/dL < 100",
    "HDL Cholesterol 52 mg/dL > 40",
    "Hemoglobin 13.5 g/dL 13.0 - 17.0",
    "Vitamin Z 9 ng/mL 1 - 5",
    "Triglycerides 150",
    "Creatinine 99 mg/dL 0.6 - 1.3",
]


def make_pdf(lines: list[str]) -> bytes:
    """A one-page PDF with a real text layer, built by hand so no PDF writer is needed."""
    stream = "BT /F1 11 Tf 40 780 Td 16 TL\n" + "\n".join(f"({s}) '" for s in lines) + "\nET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


def make_png(lines: list[str]) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1100, 60 + 46 * len(lines)), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=30)
    for n, s in enumerate(lines):
        draw.text((30, 30 + 46 * n), s, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def w() -> World:
    return World()


def up(c: TestClient, data: bytes) -> Any:
    return c.post("/v1/me/lab-uploads", content=data, headers=csrf(c))


def test_only_known_markers_are_read_and_each_value_is_a_slice_of_its_line() -> None:
    found = parse_lines("\n".join(REPORT))
    by = {c.marker: c for c in found.candidates}
    assert {m.lower() for m in by} == {
        "ldl cholesterol", "hdl cholesterol", "hemoglobin", "triglycerides", "creatinine"
    }  # fmt: skip
    for c in found.candidates:
        assert c.line in "\n".join(REPORT)
        assert f"{c.value:g}" in c.line  # the number was cut out of the source, not computed
    assert found.collected_on is not None and found.collected_on.isoformat() == "2026-11-03"
    ldl = next(c for c in found.candidates if "LDL" in c.name)
    assert (ldl.value, ldl.unit, ldl.reference, ldl.problem) == (190.0, "mg/dL", "< 100", None)
    tg = next(c for c in found.candidates if c.name == "Triglycerides")
    assert tg.problem and "unit" in tg.problem  # nothing printed, nothing guessed
    cr = next(c for c in found.candidates if c.name == "Creatinine")
    assert cr.problem  # 99 mg/dL is implausible: same rule as a typed result
    assert found.unmatched_lines >= 3  # "Vitamin Z" and headings are counted, never guessed


def test_pdf_with_a_text_layer_needs_no_ocr(w: World) -> None:
    c = w.client()
    w.signup(c)
    r = up(c, make_pdf(REPORT))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["method"] == "pdf_text" and body["collected_on"] == "2026-11-03"
    assert w.publisher.sent == []  # reading stores nothing


def test_confirm_saves_only_the_ticked_rows_and_revalidates(w: World) -> None:
    c = w.client()
    w.signup(c)
    body = up(c, make_pdf(REPORT)).json()
    rows = body["rows"]
    ldl = next(i for i, r in enumerate(rows) if r["name"].startswith("LDL"))
    hb = next(i for i, r in enumerate(rows) if r["name"] == "Hemoglobin")
    url = f"/v1/me/lab-uploads/{body['upload_id']}/confirm"
    bad = c.post(
        url,
        json={"collected_on": "2026-11-03", "rows": [{"i": hb, "value": 900, "unit": "g/dL"}]},
        headers=csrf(c),
    )
    assert bad.status_code == 422 and "Hemoglobin" in bad.json()["detail"]
    assert w.publisher.sent == []  # one bad row saves nothing
    ok = c.post(
        url,
        json={"collected_on": "2026-11-03", "rows": [{"i": ldl, "value": 190, "unit": "mg/dL"}]},
        headers=csrf(c),
    )
    assert ok.status_code == 202 and ok.json() == {"saved": 1}
    ((meta, payload),) = [
        PlainSerde().deserialize_event(p.topic, p.value) for p in w.publisher.sent
    ]
    assert meta.type == "lab.result.recorded" and payload.numeric.value == 190
    assert (
        c.post(
            url,
            json={
                "collected_on": "2026-11-03",
                "rows": [{"i": ldl, "value": 190, "unit": "mg/dL"}],
            },
            headers=csrf(c),
        ).status_code
        == 404
    )  # used once


def test_future_date_and_bad_files_are_refused(w: World) -> None:
    c = w.client()
    w.signup(c)
    assert up(c, b"just some text").status_code == 422
    assert up(c, b"%PDF-1.4 broken").status_code == 422
    body = up(c, make_pdf(REPORT)).json()
    r = c.post(
        f"/v1/me/lab-uploads/{body['upload_id']}/confirm",
        json={"collected_on": "2999-01-01", "rows": [{"i": 0, "value": 1, "unit": "mg/dL"}]},
        headers=csrf(c),
    )
    assert r.status_code == 422


def test_uploads_are_own_data_only_and_need_a_login(w: World) -> None:
    anon = w.client()
    assert anon.post("/v1/me/lab-uploads", content=b"x").status_code in (401, 403)
    a, b = w.client(), w.client()
    w.signup(a)
    w.signup(b, email="b@example.org")
    body = up(a, make_pdf(REPORT)).json()
    r = b.post(
        f"/v1/me/lab-uploads/{body['upload_id']}/confirm",
        json={"collected_on": "2026-11-03", "rows": [{"i": 0, "value": 190, "unit": "mg/dL"}]},
        headers=csrf(b),
    )
    assert r.status_code == 404
    # and a lab upload id cannot be used to save measurements
    assert b.post(f"/v1/me/imports/{body['upload_id']}/confirm", headers=csrf(b)).status_code == 404


def test_a_scan_without_an_ocr_engine_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(ExtractRefused, match="OCR"):
        labextract.read_text(make_png(["LDL Cholesterol 190 mg/dL < 100"]))


@pytest.mark.skipif(
    shutil.which("tesseract") is None
    and not __import__("os").path.exists(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
    reason="Tesseract is not installed",
)
def test_a_photo_of_a_report_is_read_by_ocr(w: World) -> None:
    c = w.client()
    w.signup(c)
    r = up(c, make_png(REPORT[3:6]))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["method"] == "ocr"
    got = {row["marker"].lower(): row["value"] for row in body["rows"]}
    assert got.get("ldl cholesterol") == 190 and got.get("hemoglobin") == 13.5
