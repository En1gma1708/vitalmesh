"""The dashboard must escape every server-supplied string it puts into HTML (Phase 7, D96).

An audit with REA (a real browser against our own app) found that lab names, units, ranges, plan
titles and recommendation text were inserted with ``innerHTML`` unescaped. A lab whose name was
``<img src=x onerror=...>`` became a real element; only the Content-Security-Policy stopped the
script. This test reads the page source and fails if a placeholder inside an HTML-producing
template literal touches a server object without being wrapped in ``esc(...)`` (or a reviewed
numeric helper), so the bug cannot come back quietly. Placeholders in plain-text template literals
(no tags) are not checked: they go through ``textContent``, ``setAttribute`` or ``.value``, which do
not parse HTML. This is a static guard, not a substitute for the browser check in the audit.
"""

from __future__ import annotations

import re
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "src" / "vitalmesh_api" / "static" / "app.html"
# names the page uses for objects that came from the server
SERVER_OBJECTS = re.compile(r"\b(?:l|m|i|it|s|a|x|view|rec|plan|labs|items|steps|data|j|p|row|r)\.")
INNERMOST = re.compile(r"\$\{([^${}]*)\}")
HTML_IN_LITERAL = re.compile(r"`[^`]*<[a-zA-Z/][^`]*`|'<[a-zA-Z/]|\"<[a-zA-Z/]")
# reviewed: these produce numbers or dates, never free text
SAFE = re.compile(
    r"^\s*(?:esc\(|rich\(|answerHtml\(|Math\.(?:round|max|min)\(|encodeURIComponent\(|"
    r"fmtDay\(|last\(s\.points\)\.v\b|s\.points\.length\b|[xy]\([^)]*\)\.toFixed\(\d\))"
)


def html_placeholders() -> list[tuple[int, str]]:
    out = []
    for no, line in enumerate(PAGE.read_text(encoding="utf-8").splitlines(), start=1):
        if not HTML_IN_LITERAL.search(line):
            continue
        for m in INNERMOST.finditer(line):
            out.append((no, m.group(1)))
    return out


def test_the_scan_sees_the_html_placeholders() -> None:
    found = html_placeholders()
    assert len(found) > 20
    assert any("esc(l.name)" in e for _, e in found)  # the lab row, the case the audit found


def test_every_placeholder_that_touches_server_data_is_escaped() -> None:
    unsafe = [
        f"line {no}: ${{{expr}}}"
        for no, expr in html_placeholders()
        if SERVER_OBJECTS.search(expr) and not SAFE.match(expr)
    ]
    assert not unsafe, "unescaped server data in HTML: " + "; ".join(unsafe)


def test_the_escape_helper_covers_the_five_html_special_characters() -> None:
    src = PAGE.read_text(encoding="utf-8")
    line = next(ln for ln in src.splitlines() if ln.startswith("const esc ="))
    for ch in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert ch in line


def test_the_guard_would_have_caught_the_original_bug() -> None:
    """Run the same check on the pre-fix line: it must be reported."""
    original = '`<td>${l.name}</td><td>${l.value ?? l.text} ${l.unit ?? ""}</td>`'
    bad = [
        m.group(1)
        for m in INNERMOST.finditer(original)
        if SERVER_OBJECTS.search(m.group(1)) and not SAFE.match(m.group(1))
    ]
    assert bad == ["l.name", "l.value ?? l.text", 'l.unit ?? ""']
