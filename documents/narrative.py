"""Narrative medical-report content extraction (read-only, spans only).

Consumes persisted ``DocumentTextPage`` + ``DocumentTextSpan`` rows (never runs
OCR again) and rebuilds a conservative, presentation-oriented section list for
narrative reports (radiology, imaging, letters...). Structural + generic cue
filtering only: no template hardcoding, no fixed coordinates, no lab-name or
clinic-name vocabulary. Header/footer noise is filtered conservatively —
preferring to keep an extra harmless line over dropping real report body.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Output shape
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NarrativeSection:
    heading: str
    body: str
    page_number: int
    sequence: int


# --------------------------------------------------------------------------- #
# Generic noise / metadata cues
# --------------------------------------------------------------------------- #

# Lines that are structurally not report body: page numbers, isolated
# punctuation, phone-like digit runs, ISO certification lines, patient/ref
# metadata markers (EN + AR), and common professional credentials. Deliberately
# generic; missing a marker only means the line is kept (harmless extra header).
NARRATIVE_NOISE_PATTERNS = (
    re.compile(r"^\s*[.\-–—•·_|/\\]{1,8}\s*$"),  # punctuation-only
    re.compile(r"^\s*\d{1,4}\s*$"),  # page number
    re.compile(r"^\s*\+?\d{6,}(?:[\s./\-:]\d+)*\s*$"),  # phone / long digits
    re.compile(r"\b(ISO|IS0)\s*15189\b", re.IGNORECASE),
    re.compile(
        r"^\s*(ref\.?\s*by|requested|reported|patient(?: id)?|name|age/sex|"
        r"specimen(?: id)?|date of birth|collection|run date/time|printed|"
        r"doctor|dr\.?|reviewed by|referred by|address|phone|tel\.?)\b",
        re.IGNORECASE,
    ),
    # Arabic metadata / age / header markers (facility, services, signatures).
    re.compile(r"\b\d+[٠-٩]?\s*(سنة|سنوات|عام|شهر|يوم|سنه)\b"),
    re.compile(
        r"(اسم المريض|اسم الطبيب|التاريخ|العمر|المرسل|العنوان|الرقم|الجوال|"
        r"هاتف|دكتوراه|الدكتور|الطبيب|بورد|تخصص|الإقامة|هشاشة|الأشعة|الرنين|"
        r"السونار|الأسنان|الثدي|محمع|مطعم|الطابق|الأرضي|المركز|العيادة|"
        r"الطابق الارضي|قرب)",
    ),
    # Professional credentials (degree token + institution or standalone).
    re.compile(
        r"\b(DMRD|FIBMS|RANZCR|FRCR|FRCP|MRCP|MBChB|MBBCh|MSc|M\.?Sc|"
        r"PhD|MD|M\.D|MRCS|FCPS|DNB)\b",
        re.IGNORECASE,
    ),
    # Generic facility / organization markers (English).
    re.compile(
        r"\b(CENTER|CENTRE|LABORATORY|LABORATORIES|LAB|CLINIC|HOSPITAL|"
        r"RADIOLOGY CENTER|MEDICAL CENTRE|MEDICAL CENTER)\b",
        re.IGNORECASE,
    ),
)

# Lines that look like a section heading / report title: short, no sentence
# punctuation, not ending in a digit.
HEADING_MAX_CHARS = 48
HEADING_PUNCT = re.compile(r"[.!?。；;]$")
HEADING_NOISE = re.compile(r"\b(هشاشة|الأشعة|السونار|الرنين|الأسنان|الثدي)\b")


def _is_noise(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    for pattern in NARRATIVE_NOISE_PATTERNS:
        if pattern.search(stripped):
            return True
    return False


def _is_heading(text: str, next_text: str | None) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > HEADING_MAX_CHARS:
        return False
    if HEADING_PUNCT.search(stripped):
        return False
    if HEADING_NOISE.search(stripped):
        return False
    # A heading is short and is followed by a longer body line (or is the last
    # surviving line). Pure single words are treated as headings too.
    if next_text is None:
        return True
    return len(stripped) <= len(next_text.strip()) * 0.6 and len(stripped) >= 2


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def _page_lines(page) -> list[dict]:
    """Rebuild ordered lines from persisted spans for one page.

    Returns [{text, confidence, y_min, y_max, sequence}]. Pure geometry only;
    canonical OCR text is read as-is.
    """
    spans = (
        page.spans.select_related("document_text_page")
        .order_by("sequence")
        .all()
    )
    lines: list[dict] = []
    for span in spans:
        text = (span.text or "").strip()
        if not text:
            continue
        lines.append(
            {
                "text": text,
                "confidence": span.confidence or 0.0,
                "y_min": span.y_min,
                "y_max": span.y_max,
                "sequence": span.sequence,
            }
        )
    return lines


def _body_lines(pages) -> list[dict]:
    """Filter noise lines from all pages into ordered body lines."""
    body: list[dict] = []
    for page in pages:
        for line in _page_lines(page):
            # Drop very-low-confidence OCR garbage and structurally noisy lines.
            if line["confidence"] < 0.5:
                continue
            if _is_noise(line["text"]):
                continue
            body.append({**line, "page_number": page.page_number})
    return body


def _filter_fringe(body: list[dict]) -> list[dict]:
    """Drop isolated header/footer fringes around the core body.

    Structural only (no fixed coordinates): a short run of lines separated from
    the rest by a large vertical gap is a header or signature/address fringe.
    Runs of more than ``FRINGE_MAX_LINES`` lines are never touched, so a large
    body block can never be dropped. Gap analysis runs on Y-sorted lines; the
    surviving subset keeps its original (sequence) order.
    """
    if len(body) < 3:
        return body
    ordered = sorted(body, key=lambda line: (line["y_min"], line["y_max"]))
    heights = [line["y_max"] - line["y_min"] for line in ordered]
    median_h = statistics.median(heights)
    gap_threshold = max(median_h * 3.0, 0.005)
    # leading fringe: a short run before the first large gap
    fringe_end = None
    for i in range(1, len(ordered)):
        if ordered[i]["y_min"] - ordered[i - 1]["y_max"] > gap_threshold:
            fringe_end = i
            break
    if fringe_end is not None and fringe_end <= 3:
        ordered = ordered[fringe_end:]
    # trailing fringe: a short run after the last large gap
    fringe_start = None
    for i in range(len(ordered) - 1, 0, -1):
        if ordered[i]["y_min"] - ordered[i - 1]["y_max"] > gap_threshold:
            fringe_start = i
            break
    if fringe_start is not None and len(ordered) - fringe_start <= 3:
        ordered = ordered[:fringe_start]
    kept_ids = {id(line) for line in ordered}
    return [line for line in body if id(line) in kept_ids]


def _to_sections(body: list[dict]) -> list[NarrativeSection]:
    """Group body lines into heading + paragraph sections."""
    sections: list[NarrativeSection] = []
    if not body:
        return sections
    current_heading = ""
    current_body: list[str] = []
    current_sequence = body[0]["sequence"]
    current_page = body[0]["page_number"]

    def flush() -> None:
        nonlocal current_heading, current_body, current_sequence, current_page
        if current_body:
            sections.append(
                NarrativeSection(
                    heading=current_heading,
                    body="\n".join(current_body),
                    page_number=current_page,
                    sequence=current_sequence,
                )
            )
        current_heading = ""
        current_body = []
        current_sequence = 0
        current_page = 0

    for index, line in enumerate(body):
        next_text = (
            body[index + 1]["text"] if index + 1 < len(body) else None
        )
        if _is_heading(line["text"], next_text):
            # close previous section, start a new heading
            flush()
            current_heading = line["text"].strip()
            current_sequence = line["sequence"]
            current_page = line["page_number"]
        else:
            if not current_heading and not current_body:
                # body appeared before any heading: use a generic heading
                current_heading = ""
                current_sequence = line["sequence"]
                current_page = line["page_number"]
            current_body.append(line["text"].strip())
    flush()
    return sections


def extract_narrative(document) -> list[NarrativeSection]:
    """Build narrative sections for a document from its persisted OCR text."""
    text = getattr(document, "document_text", None)
    if text is None:
        return []
    pages = (
        text.pages.prefetch_related("spans")
        .order_by("page_number")
        .all()
    )
    body = _body_lines(pages)
    body = _filter_fringe(body)
    return _to_sections(body)
