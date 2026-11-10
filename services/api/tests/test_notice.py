"""The optional banner for a shared demo (D99): shown on every page, escaped, absent by default."""

from __future__ import annotations

import pytest
from test_security import World
from vitalmesh_api.main import NOTICE_MAX, notice_from_env

TEXT = "Demo: please use made-up data only."
PAGES = ["/", "/app", "/ops", "/clinician", "/reset"]


@pytest.mark.parametrize("path", PAGES)
def test_the_banner_is_on_every_page(path: str) -> None:
    html = World(notice=TEXT).client().get(path).text
    assert TEXT in html
    assert html.index(TEXT) < html.index("</body>")
    assert html.index("<body>") < html.index(TEXT)  # at the top, not buried


@pytest.mark.parametrize("path", PAGES)
def test_there_is_no_banner_by_default(path: str) -> None:
    html = World().client().get(path).text
    assert 'role="note"' not in html


def test_the_banner_text_is_escaped_not_interpreted() -> None:
    evil = '<img src=x onerror=alert(1)><script>alert(2)</script> & "quotes"'
    html = World(notice=evil).client().get("/").text
    assert "<img src=x" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert html.count("<script>alert(2)") == 0


def test_the_nonce_is_still_added_with_a_banner() -> None:
    html = World(notice=TEXT).client().get("/").text
    assert "<script nonce=" in html and "<style nonce=" in html


def test_notice_from_env_accepts_plain_text_and_empty() -> None:
    assert notice_from_env({}) == ""
    assert notice_from_env({"VITALMESH_NOTICE": f"  {TEXT}  "}) == TEXT


@pytest.mark.parametrize("bad", ["x" * (NOTICE_MAX + 1), "two\nlines", "tab\there", "nul\x00"])
def test_a_bad_notice_stops_the_process_at_start(bad: str) -> None:
    with pytest.raises(SystemExit):
        notice_from_env({"VITALMESH_NOTICE": bad})
