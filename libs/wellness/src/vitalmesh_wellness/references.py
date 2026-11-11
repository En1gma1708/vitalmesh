"""Reference figures for everyday answers (D108), each a verbatim quotation from an ingested
US government document, so the answer can state them as CITED claims with no model involved.

Every entry is checked by a test against the manifest (document, licence, URL) and, when the PDF
is in the local cache, against the page text itself: a quote that is not on the stated page fails
the test. A figure that has no entry here is simply not shown (no uncited range).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_US_WORK = (
    "US federal government work (17 U.S.C. 105): no US copyright. The PDF itself states no "
    "licence, so this is inferred from its authorship, not quoted"
)


@dataclass(frozen=True)
class Reference:
    key: str
    doc_id: str
    title: str
    source_url: str
    licence: str
    locator: str
    quote: str  # verbatim from the page (whitespace and dash style aside)
    sentence: str  # what the answer says; every number in it comes from the quote


SLEEP = Reference(
    key="sleep_hours",
    doc_id="nhlbi-sleep-2011",
    title="Your Guide to Healthy Sleep (NIH Publication No. 11-5271, revised August 2011)",
    source_url="https://www.nhlbi.nih.gov/files/docs/public/sleep/healthy_sleep.pdf",
    licence=_US_WORK,
    locator="p. 8",
    quote="adults need at least 7\u20138 hours of sleep each night to be well rested",
    sentence=(
        "The NHLBI sleep guide says adults need at least 7\u20138 hours of sleep each night to be "
        "well rested."
    ),
)

LOSS_RATE = Reference(
    key="loss_rate",
    doc_id="nhlbi-obesity-1998",
    title="Clinical Guidelines on the Identification, Evaluation, and Treatment of Overweight "
    "and Obesity in Adults: The Evidence Report (NIH Publication No. 98-4083, 1998)",
    source_url="https://www.nhlbi.nih.gov/files/docs/guidelines/ob_gdlns.pdf",
    licence=_US_WORK,
    locator="p. 104",
    quote=(
        "Weight loss at the rate of 1 to 2 lb/week (calorie deficit of 500 to 1,000 kcal/day) "
        "commonly occurs for up to 6 months"
    ),
    sentence=(
        "The NHLBI obesity guidelines (1998) report weight loss at the rate of 1 to 2 lb/week "
        "commonly occurring for up to 6 months."
    ),
)

BMI_BANDS = Reference(
    key="bmi_bands",
    doc_id="nhlbi-obesity-1998",
    title=LOSS_RATE.title,
    source_url=LOSS_RATE.source_url,
    licence=_US_WORK,
    locator="p. 16",
    quote="Underweight < 18.5 Normal 18.5 \u2013 24.9 Overweight 25.0 \u2013 29.9",
    sentence=(
        "In the NHLBI classification a BMI under 18.5 is underweight, 18.5 to 24.9 is normal "
        "and 25.0 to 29.9 is overweight."
    ),
)

ALL = (SLEEP, LOSS_RATE, BMI_BANDS)
BY_KEY = {r.key: r for r in ALL}


def normalise(text: str) -> str:
    """Compare quotations without caring about line breaks, spacing, or dash style."""
    t = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
    return re.sub(r"\s+", "", t).casefold()


def quote_on_page(quote: str, page_text: str) -> bool:
    return normalise(quote) in normalise(page_text)


def as_citation(ref: Reference) -> dict[str, str]:
    """The shape a specialist's tool result hands to the event builder."""
    return {
        "doc_id": ref.doc_id,
        "title": ref.title,
        "locator": ref.locator,
        "licence": ref.licence,
        "source_url": ref.source_url,
        "quote": ref.quote,
        "sentence": ref.sentence,
    }
