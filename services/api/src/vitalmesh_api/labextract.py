"""Read lab results from a blood-report PDF or a photo/scan of one (D86).

Why code and not a model: a lab report is the most sensitive file a person owns, and the project
rule is that real health data never goes to an outside LLM (D42). So extraction is deterministic:

* a PDF with a text layer is read with ``pypdf``;
* a scan or photo (PNG/JPEG, or a PDF with no text layer) goes through Tesseract on this machine;
* a line is accepted only if it STARTS with a marker name from our dictionary, so each accepted
  value is a slice of the source line (verbatim by construction; a test asserts it) and unknown
  rows are counted, never guessed.

What this does NOT catch: an OCR misread that still looks like a plausible number (a "1" read as
"7"). The plausibility and unit checks catch absurd values only, so the confirm screen shows the
source line beside every value and the person edits it. Nothing is stored until they confirm, and
confirmed rows pass through the same ``interpret`` validation as a hand-typed result.

Untrusted input: size, page, pixel and time caps; files are processed from memory, never written.
"""

from __future__ import annotations

import io
import re
import shutil
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from vitalmesh_wellness.labs import MARKERS, LabError, interpret

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

MAX_BYTES = 15 * 1024 * 1024
MAX_PAGES = 6
MAX_PIXELS = 30_000_000
OCR_TIMEOUT_S = 30
MIN_TEXT_CHARS = 40  # fewer than this on a PDF means a scan with no text layer
_OCR_SLOTS = threading.BoundedSemaphore(2)  # OCR is CPU heavy: never more than two at once


class ExtractRefused(ValueError):
    """The file cannot be read; the message is plain language for the person."""


class OcrBusy(ExtractRefused):
    """Two scans are already being read; try again shortly."""


@dataclass
class Candidate:
    name: str  # as the lab printed it
    marker: str  # our dictionary name
    value: float | None
    unit: str
    reference: str
    line: str  # the source line, shown beside the value
    problem: str | None = None  # why this row cannot be saved as it stands


@dataclass
class Extraction:
    method: str  # "pdf_text" or "ocr"
    candidates: list[Candidate] = field(default_factory=list)
    collected_on: date | None = None
    unmatched_lines: int = 0


# ----------------------------------------------------------------------------- text sources
def _pdf_text(data: bytes) -> list[str]:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ExtractRefused("That PDF is password protected. Save an unlocked copy.")
        return [(p.extract_text() or "") for p in reader.pages[:MAX_PAGES]]
    except ExtractRefused:
        raise
    except (PyPdfError, ValueError, KeyError, OSError):
        raise ExtractRefused("That PDF could not be read.") from None


def _tesseract() -> str:
    found = shutil.which("tesseract")
    if found:
        return found
    for guess in (r"C:\Program Files\Tesseract-OCR\tesseract.exe", "/usr/bin/tesseract"):
        if shutil.which(guess):
            return guess
    raise ExtractRefused("Reading scans is not available on this server (no OCR engine).")


def _ocr(images: list[PILImage]) -> list[str]:
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = _tesseract()
    if not _OCR_SLOTS.acquire(blocking=False):
        raise OcrBusy("Two scans are being read right now. Try again in a minute.")
    try:
        out = []
        for img in images:
            try:
                out.append(
                    pytesseract.image_to_string(
                        img.convert("L"), timeout=OCR_TIMEOUT_S, config="--psm 6"
                    )
                )
            except RuntimeError:  # pytesseract raises RuntimeError on timeout
                raise ExtractRefused(
                    "Reading that scan took too long. Try a smaller image."
                ) from None
        return out
    finally:
        _OCR_SLOTS.release()


def _render_pdf(data: bytes) -> list[PILImage]:
    import pypdfium2

    try:
        pdf = pypdfium2.PdfDocument(data)
        return [pdf[i].render(scale=2.5).to_pil() for i in range(min(len(pdf), MAX_PAGES))]
    except Exception:  # pdfium raises several types for damaged files
        raise ExtractRefused("That PDF could not be opened for reading.") from None


def _open_image(data: bytes) -> PILImage:
    from PIL import Image, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_PIXELS:
            raise ExtractRefused("That image is too large. Use a smaller photo.")
        img.load()
        return img
    except ExtractRefused:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        raise ExtractRefused("That image could not be read.") from None


def read_text(data: bytes) -> tuple[str, str]:
    """Return (method, text). The kind of file is decided by its bytes, not its name."""
    if len(data) > MAX_BYTES:
        raise ExtractRefused("That file is bigger than 15 MB.")
    if data[:5] == b"%PDF-":
        pages = _pdf_text(data)
        if sum(len(p.strip()) for p in pages) >= MIN_TEXT_CHARS:
            return "pdf_text", "\n".join(pages)
        return "ocr", "\n".join(_ocr(_render_pdf(data)))
    if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff":
        return "ocr", "\n".join(_ocr([_open_image(data)]))
    raise ExtractRefused("Use a PDF, a PNG or a JPEG of the report.")


# --------------------------------------------------------------------------------- parsing
_NAMES = sorted(
    ((n, m.name) for m in MARKERS for n in m.names), key=lambda pair: len(pair[0]), reverse=True
)
_ROW = re.compile(
    r"^[\s:.\-]*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>(?:[a-zA-Zµμ%][\w%/.\^µμ]*))?"
    r"(?:\s+(?P<ref>(?:<=?|>=?)?\s*\d+(?:\.\d+)?(?:\s*(?:-|\u2013|\u2014|to)\s*\d+(?:\.\d+)?)?))?"
)
_DATE_WORDS = r"(?:collected|collection|sample|specimen|drawn|reported|date)"
_ISO = re.compile(rf"{_DATE_WORDS}[^\n\d]{{0,25}}(\d{{4}})-(\d{{2}})-(\d{{2}})", re.I)
_DMY = re.compile(rf"{_DATE_WORDS}[^\n\d]{{0,25}}(\d{{1,2}})[/.-](\d{{1,2}})[/.-](\d{{4}})", re.I)


def find_date(text: str) -> date | None:
    """Prefill only; the person confirms the date. Day first (dd/mm/yyyy) as on Indian reports."""
    try:
        if m := _ISO.search(text):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if m := _DMY.search(text):
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
    return None


def check(name: str, value: float, unit: str, reference: str) -> str | None:
    """The same rules a hand-typed result passes: unit known for the marker, value plausible."""
    from datetime import UTC, datetime

    try:
        interpret(name, datetime.now(UTC), value=value, unit=unit, reference_text=reference)
    except LabError as exc:
        return str(exc)
    return None


def parse_lines(text: str) -> Extraction:
    out = Extraction("text")
    out.collected_on = find_date(text)
    seen: set[str] = set()
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        low = line.lower()
        hit = next(((n, m) for n, m in _NAMES if low.startswith(n + " ") or low == n), None)
        if hit is None:
            out.unmatched_lines += 1
            continue
        printed, marker = hit
        rest = line[len(printed) :]
        row = _ROW.match(rest)
        if row is None:
            out.unmatched_lines += 1
            continue
        if marker in seen:  # a report repeats a marker in a summary: keep the first
            continue
        seen.add(marker)
        shown = line[: len(printed)]
        value = float(row.group("value"))
        unit = row.group("unit") or ""
        reference = (row.group("ref") or "").strip()
        problem = None if unit else "No unit was printed on this line. Type it in."
        if problem is None:
            problem = check(shown, value, unit, reference)
        out.candidates.append(Candidate(shown, marker, value, unit, reference, line, problem))
    return out


def extract(data: bytes) -> Extraction:
    method, text = read_text(data)
    out = parse_lines(text)
    out.method = method
    if not out.candidates:
        raise ExtractRefused(
            "I could not find any known lab results in that file. "
            "You can still type results in by hand."
        )
    return out
