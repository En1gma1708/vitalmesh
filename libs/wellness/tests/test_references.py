"""Reference quotations match the corpus manifest and, when cached, the page text (D108)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from vitalmesh_wellness.references import ALL, as_citation, normalise, quote_on_page

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = json.loads((ROOT / "libs/retrieval/corpus/manifest.json").read_text(encoding="utf-8"))
DOCS = {d["doc_id"]: d for d in MANIFEST["documents"]}
CACHE = ROOT / ".cache" / "corpus"


@pytest.mark.parametrize("ref", ALL, ids=lambda r: r.key)
def test_every_reference_points_at_a_manifest_document(ref) -> None:  # type: ignore[no-untyped-def]
    doc = DOCS[ref.doc_id]
    assert ref.source_url == doc["source_url"]
    assert ref.licence.split(":")[0] == doc["licence"].split(":")[0]
    assert ref.locator.startswith("p. ")


@pytest.mark.parametrize("ref", ALL, ids=lambda r: r.key)
def test_the_quote_is_on_the_stated_page_of_the_cached_pdf(ref) -> None:  # type: ignore[no-untyped-def]
    pdf = CACHE / f"{ref.doc_id}.pdf"
    if not pdf.exists():
        pytest.skip("corpus PDF not cached here (scripts/build_corpus.py downloads it)")
    from pypdf import PdfReader

    logging.disable(logging.CRITICAL)
    page = int(ref.locator.removeprefix("p. "))
    text = PdfReader(str(pdf)).pages[page - 1].extract_text() or ""
    assert quote_on_page(ref.quote, text), ref.key


def test_the_numbers_in_a_sentence_come_from_its_quote() -> None:
    import re

    for ref in ALL:
        nums = set(re.findall(r"\d+(?:\.\d+)?", ref.sentence))
        quote_nums = set(re.findall(r"\d+(?:\.\d+)?", ref.quote))
        # the 1998 date and the page are context, not figures; everything else must be quoted
        assert nums - {"1998"} <= quote_nums, (ref.key, nums - quote_nums)


def test_normalise_ignores_spacing_and_dash_style() -> None:
    assert normalise("7\u20138 hours") == normalise("7 - 8  hours")
    # PDF extraction splits words ("lb/w eek"), so the comparison ignores all spacing
    assert quote_on_page("lb/week", "1 to 2 lb/w eek")
    assert not quote_on_page("lb/day", "1 to 2 lb/w eek")


def test_as_citation_has_every_field_the_event_needs() -> None:
    c = as_citation(ALL[0])
    assert set(c) == {"doc_id", "title", "locator", "licence", "source_url", "quote", "sentence"}
