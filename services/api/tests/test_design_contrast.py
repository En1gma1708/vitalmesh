"""D106: the colours in vm.css meet WCAG AA, measured from the real tokens (not from a copy).

People with health problems (low vision, migraine, tired eyes) use this product, so a colour
change that drops a text, button or chart pair below the threshold fails here. Chart marks
(lines, bars, rings) need 3:1; text needs 4.5:1.
"""

from __future__ import annotations

import re

import pytest
from vitalmesh_api.app import STATIC

CSS = (STATIC / "vm.css").read_text(encoding="utf-8")
HEX = re.compile(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\b")


def _block(selector: str) -> str:
    start = CSS.index(selector)
    return CSS[CSS.index("{", start) + 1 : CSS.index("\n}", start)]


def _tokens(*selectors: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for sel in selectors:
        out.update({k: v for k, v in HEX.findall(_block(sel))})
    return out


PALETTES = {"base": _tokens(":root {")}
if '[data-palette="forest"]' in CSS:
    PALETTES["forest"] = _tokens(":root {", ':root[data-palette="forest"]')


def _lum(h: str) -> float:
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (1, 3, 5))

    def f(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


TEXT_PAIRS = [
    ("fg", "bg"), ("fg", "card"), ("fg", "card-2"),
    ("muted", "bg"), ("muted", "card"), ("muted", "card-2"), ("muted", "rail"),
    ("on-accent", "accent"), ("accent", "card"), ("accent", "bg"), ("accent", "accent-soft"),
    ("warn", "card"), ("warn", "warn-soft"), ("on-warn", "warn"),
    ("ok", "card"),
]  # fmt: skip
MARK_PAIRS = [("c-sleep", "card"), ("c-active", "card"), ("c-energy", "card"), ("c-weight", "card"),
              ("avg", "card"), ("warn", "card")]  # fmt: skip


@pytest.mark.parametrize("palette", sorted(PALETTES))
def test_text_pairs_meet_aa(palette: str) -> None:
    t = PALETTES[palette]
    for fg, bg in TEXT_PAIRS:
        assert ratio(t[fg], t[bg]) >= 4.5, f"{palette}: {fg} on {bg} is {ratio(t[fg], t[bg]):.2f}"


@pytest.mark.parametrize("palette", sorted(PALETTES))
def test_chart_marks_are_visible_on_the_card(palette: str) -> None:
    t = PALETTES[palette]
    for fg, bg in MARK_PAIRS:
        assert ratio(t[fg], t[bg]) >= 3.0, f"{palette}: {fg} on {bg} is {ratio(t[fg], t[bg]):.2f}"


def test_the_page_is_light_not_dark() -> None:
    t = PALETTES["base"]
    assert _lum(t["bg"]) > 0.8 and _lum(t["card"]) > 0.8  # a light page, on purpose
