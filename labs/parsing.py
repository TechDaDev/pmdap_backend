"""Geometry-backed structured lab result parsing.

Pure functions over normalized spans (0.0-1.0 coordinates); no DB, no OCR, no
template hardcoding. The parser uses generic cues (column headers, row
proximity, numeric-result patterns, unit shapes) so it works across report
layouts. It never derives clinical meaning — printed flags are recorded as
``flag_raw`` only.

Consumes persisted OCR spans (never runs OCR again).
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.conf import settings

# --------------------------------------------------------------------------- #
# Input / output data shapes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Span:
    page_number: int
    sequence: int
    text: str
    confidence: float
    x_min: float
    y_min: float
    x_max: float
    y_max: float


@dataclass(frozen=True)
class ParsedResult:
    page_number: int
    row_index: int
    test_name_raw: str
    result_raw: str
    result_numeric: Decimal | None
    result_text: str
    unit_raw: str
    reference_range_raw: str
    reference_low: Decimal | None
    reference_high: Decimal | None
    flag_raw: str
    extraction_confidence: float
    evidence_sequence: tuple[int, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# Cues / patterns
# --------------------------------------------------------------------------- #

HEADER_CUES = {
    "item",
    "test",
    "tests",
    "parameter",
    "analysis",
    "component",
    "investigation",
    "result",
    "results",
    "value",
    "values",
    "unit",
    "units",
    "reference",
    "reference range",
    "reference range (adult)",
    "reference range (adult male)",
    "ref range",
    "ref. range",
    "referencerange",
    "ref",
    "range",
    "normal range",
    "normal",
    "normal value",
    "flags",
    "flag",
    "low",
    "high",
    "low/high",
}

SECTION_CUES = {
    "chemistry",
    "diabetes profile",
    "lipid profile",
    "liver profile",
    "liver function",
    "renal function",
    "kidney profile",
    "kidney function",
    "renal profile",
    "vitamins",
    "hormones",
    "pituitary hormones",
    "thyroid hormones",
    "thyroid profile",
    "tumor markers",
    "tumour markers",
    "haematology",
    "hematology",
    "cbc",
    "full blood count",
    "fbc",
    "coagulation",
    "serology",
    "immunology",
    "electrolytes",
    "bone profile",
    "iron studies",
    "protein",
    "cardiac markers",
    "urinalysis",
}

# Metadata rows that must never become lab results (patient id, dates, phones,
# report ids, ISO certs, addresses, doctor/lab headers...).
ROW_METADATA_PATTERNS = (
    re.compile(r"^\s*(patient id|patient|name|ref\.?\s*by|requested|reported|age/sex|age|sex|specimen id|specimen|test:|sequence|location|physician|comments|gender|date of birth|collection|run date/time|printed|doctor|dr\.?|reviewed by|phone|tel\.?|mobile|身份证)\b", re.IGNORECASE),
    re.compile(r"\b(ISO|IS0)\s*15189\b", re.IGNORECASE),
    re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{2,4}\s*$"),
    re.compile(r"^\s*\d{1,2}-\d{1,2}-\d{2,4}\s*$"),
    re.compile(r"^\s*\+?\d{7,}\s*$"),
    re.compile(r"^\s*(id|no\.?|no)\s*[:#]?\s*\d+(\s|$)", re.IGNORECASE),
    re.compile(r"^\s*(ref\.?\s*by|dr\.?|d\.?r\.?)\s", re.IGNORECASE),
    re.compile(r"^\s*[أ-ي]{2,}\s*$"),  # bare Arabic word(s) w/o digits -> header/footer
)

CONTINUATION_CUES = re.compile(
    r"^\s*(normal|reference|ref|n\.?v|pre|post|borderline|optimal|deficiency|insufficiency|sufficiency|toxicity|diabetic|adult|child|age|\d+\s*(?:year|years|y|yr|mo|m|d)|<|>|≤|≥|up to|less than|greater than|high|low|normal range)",
    re.IGNORECASE,
)

FLAG_RE = re.compile(r"^[hlr*]{1,2}$", re.IGNORECASE)


def _is_flag(text: str) -> bool:
    stripped = text.strip()
    return 1 <= len(stripped) <= 2 and bool(FLAG_RE.match(stripped))

# Known unit vocabulary (normalized lowercase). Shape alone is too loose: any
# alphabetic word would otherwise look like a unit ("negative", "glucose").
KNOWN_UNITS = {
    "mg/dl", "mg/ml", "ng/ml", "ng/dl", "g/dl", "g/l", "ug/dl", "ug/ml",
    "pg/ml", "pg/dl", "u/l", "iu/l", "muiu/ml", "uiu/ml", "uiu/l", "miu/l",
    "miu/ml", "u/ml", "iu/ml", "%", "µl", "ul", "fl", "mmol/l", "umol/l",
    "nmol/l", "pmol/l", "mmol/ml", "meq/l", "mcg/dl", "mm/h", "mg", "ng",
    "pg", "g", "ug", "u", "iu", "x10^3/µl", "x10^3/ul", "x103/µl",
    "x103/ul", "x106/µl", "10^3/µl", "10^3/ul", "cells/µl", "cells/ul",
    "u/dl", "u/", "g/100ml",
}

UNIT_SHAPE = re.compile(
    r"^(?:x10\^?\d*/?[a-zµμ%]*|10\^?\d*/?[a-zµμ%]*|[a-zµμ%]+/[a-zµμ%]+|[a-zµμ%]{1,3}|%)$",
    re.IGNORECASE,
)


def _is_unit(text: str) -> bool:
    key = text.strip().lower().replace(" ", "")
    if key in KNOWN_UNITS:
        return True
    # conservative: short unit-shaped tokens only (u/l, %, µL, fL); long prose
    # words like "negative" or "glucose" never count as units
    return len(key) <= 4 and bool(UNIT_SHAPE.match(key))


# OCR-degraded cell-count units (x10^3/µL -> "x10%μL", "x103/µL" ...) must be
# preserved; the token is Latin/symbol/digit only, so Arabic watermark strokes
# and prose never match. Requires a digit or a µ/μ glyph to avoid a bare Latin
# word ("LAB", "RBC") being mistaken for a unit.
UNIT_CELL_LENIENT_RE = re.compile(
    r"^[a-zA-Zµμ×x0-9/^%·.\-]{1,10}$", re.UNICODE
)


def _is_unit_cell(text: str) -> bool:
    stripped = text.strip()
    if _is_unit(stripped):
        return True
    if not UNIT_CELL_LENIENT_RE.match(stripped):
        return False
    # Requires a digit, a µ/μ glyph, or a slash (x/y shape) so a bare Latin
    # word ("LAB", "RBC") is never mistaken for a unit, while OCR-degraded
    # forms like "x10%μL" or "ulU/ml" (µIU/ml) are preserved.
    return (
        any(ch.isdigit() for ch in stripped)
        or "μ" in stripped
        or "µ" in stripped
        or "/" in stripped
    )


def _is_cell_like(text: str) -> bool:
    """True when a span could plausibly be table content (value/unit/flag/range).

    Used to guard the geometric noise filter so oversized non-table strokes
    (diagonal watermarks, stamps, signatures) are dropped from lab rows while
    legitimate numeric/unit/range cells are never touched.
    """
    stripped = text.strip()
    if _is_unit_cell(stripped) or _is_flag(stripped):
        return True
    return bool(
        DECIMAL_RE.match(stripped)
        or THRESHOLD_RE.match(stripped)
        or RANGE_RE.match(stripped)
        or RANGE_ANYWHERE_RE.search(stripped)
    )


def _drop_geometric_noise(row: list[Span]) -> list[Span]:
    """Drop diagonal watermark / stamp strokes from a row before assembly.

    Structural only (no vocabulary, no lab-name knowledge, no language): a
    rotated watermark has an OCR box far taller than the row's normal text and
    is never a value/unit/flag/range cell. Such a stroke is excluded from
    LabResult association (canonical DocumentTextSpan rows are untouched).
    Legitimate cells are never oversized-and-cell-like, so numeric/unit/range
    content is preserved. Only rows with >= 3 spans are filtered so an isolated
    large token (e.g. a title) is not nuked.
    """
    if len(row) < 3:
        return row
    heights = [span.y_max - span.y_min for span in row]
    median_h = statistics.median(heights)
    if median_h <= 0:
        return row
    threshold = median_h * 1.6
    cleaned: list[Span] = []
    for span in row:
        oversized = (span.y_max - span.y_min) > threshold
        if oversized and not _is_cell_like(span.text):
            continue
        cleaned.append(span)
    return cleaned

DECIMAL_RE = re.compile(r"^[+-]?\d{1,6}(?:[.,]\d{1,4})?$")

# Long digit runs (phone numbers, big IDs) are never lab results.
PHONE_LIKE_RE = re.compile(r"\+?\d{7,}(?:[\s-]\d+)*$")

RANGE_RE = re.compile(
    r"^(<?\s*(\d+(?:[.,]\d+)?))\s*-\s*(>?\s*(\d+(?:[.,]\d+)?))$"
)

# Unanchored range anywhere in a cell (wrapped/annotated reference ranges).
RANGE_ANYWHERE_RE = re.compile(r"\d+(?:[.,]\d+)?\s*[-–]\s*<?\s*\d+(?:[.,]\d+)?")

THRESHOLD_RE = re.compile(r"^(<|<=|>|>=|≤|≥)\s*(\d+(?:[.,]\d+)?)$")

REFERENCE_TEXT_CUES = re.compile(
    r"(normal|reference|n\.?v|pre diabetic|diabetic|optimal|borderline|deficiency|insufficiency|sufficiency|toxicity|less than|greater than|up to|high risk|adult|child|\byear|\bmo\b|mg/dL|ng/mL|%)",
    re.IGNORECASE,
)

SPECIAL_RESULT = {
    "negative",
    "positive",
    "detected",
    "not detected",
    "indeterminate",
    "trace",
    "none",
    "nil",
    "pending",
    "see comment",
}


def _norm(text: str) -> str:
    """Lowercase, strip punctuation/whitespace for cue matching only."""
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9أ-ي ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _to_decimal(value: str) -> Decimal | None:
    cleaned = value.strip().replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _center(span: Span) -> tuple[float, float]:
    return (
        (span.x_min + span.x_max) / 2,
        (span.y_min + span.y_max) / 2,
    )


# --------------------------------------------------------------------------- #
# Row grouping (Y proximity)
# --------------------------------------------------------------------------- #


def group_rows(spans: list[Span], *, tolerance_factor: float = 0.6) -> list[list[Span]]:
    """Group spans into horizontal rows by Y-center proximity.

    Robust to small per-cell Y offsets within one row and to slightly ragged
    tops/bottoms. Rows are returned in reading order, each row sorted by X.
    """
    if not spans:
        return []
    ordered = sorted(spans, key=lambda s: (s.y_min + s.y_max) / 2)
    heights = [s.y_max - s.y_min for s in ordered]
    median_height = statistics.median(heights) if heights else 0.001
    tolerance = max(median_height * tolerance_factor, 1e-4)
    rows: list[list[Span]] = []
    for span in ordered:
        cy = (span.y_min + span.y_max) / 2
        placed = False
        for row in rows:
            centers = [(c.y_min + c.y_max) / 2 for c in row]
            mean_cy = sum(centers) / len(centers)
            if abs(cy - mean_cy) <= tolerance:
                row.append(span)
                placed = True
                break
        if not placed:
            rows.append([span])
    return [sorted(row, key=lambda s: s.x_min) for row in rows]


# --------------------------------------------------------------------------- #
# Header detection & column mapping
# --------------------------------------------------------------------------- #


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            insert = current[j - 1] + 1
            delete = previous[j] + 1
            substitute = previous[j - 1] + (ca != cb)
            current.append(min(insert, delete, substitute))
        previous = current
    return previous[-1]


def _header_key(text: str) -> str | None:
    """Canonical header cue for a cell, or None.

    Exact normalized match first; then a conservative single-edit fuzzy match
    limited to short words so arbitrary long prose never becomes a header.
    """
    normed = _norm(text)
    if normed in HEADER_CUES:
        return normed
    if normed and len(normed) <= 8:
        for cue in HEADER_CUES:
            if abs(len(normed) - len(cue)) <= 1 and _levenshtein(normed, cue) <= 1:
                return cue
    return None


def _is_header_cell(text: str) -> bool:
    return _header_key(text) is not None


def _header_score(row: list[Span]) -> int:
    return sum(1 for span in row if _is_header_cell(span.text))


def find_header_row(rows: list[list[Span]]) -> int | None:
    """Index of best header row, or None when none looks like a table header."""
    best_index = None
    best_score = 0
    for index, row in enumerate(rows):
        score = _header_score(row)
        if score >= 2 and score > best_score:
            best_index = index
            best_score = score
    return best_index


def _column_bounds(header_row: list[Span]) -> list[tuple[float, float]]:
    """X bounds for each header column using bisectors between centers."""
    centers = [_center(span)[0] for span in header_row]
    bounds = []
    for index, center in enumerate(centers):
        left = -1.0
        right = 2.0
        if index > 0:
            left = (centers[index - 1] + center) / 2
        if index < len(centers) - 1:
            right = (centers[index] + centers[index + 1]) / 2
        bounds.append((left, right))
    return bounds


def _column_semantics(header_row: list[Span]) -> list[str]:
    semantics = []
    for span in header_row:
        key = _header_key(span.text) or ""
        if key in {"item", "test", "tests", "parameter", "analysis", "component"}:
            semantics.append("TEST")
        elif key in {"result", "results", "value", "values"}:
            semantics.append("RESULT")
        elif key in {"unit", "units"}:
            semantics.append("UNIT")
        elif key in {"flags", "flag"}:
            semantics.append("FLAG")
        elif key in {"low", "low/high"}:
            semantics.append("REF_LOW")
        elif key in {"high"}:
            semantics.append("REF_HIGH")
        elif key in {"reference", "reference range", "ref range", "ref. range",
                     "referencerange", "range", "normal range", "normal",
                     "normal value", "ref", "reference range (adult)",
                     "reference range (adult male)"}:
            semantics.append("REFERENCE")
        else:
            semantics.append("OTHER")
    return semantics


def _assign_column(x_center: float, bounds: list[tuple[float, float]]) -> int:
    for index, (left, right) in enumerate(bounds):
        if left <= x_center < right:
            return index
    return -1


def _split_header_groups(semantics: list[str]) -> list[list[int]]:
    """Split a wide header into sub-tables, one per TEST column.

    CBC-style reports print two (or more) six-column panels side by side
    (Test/Result/Flags/Units/Low/High twice). Each TEST column starts its own
    logical sub-table so paired panels parse as separate lab rows.
    """
    test_columns = [index for index, sem in enumerate(semantics) if sem == "TEST"]
    if len(test_columns) <= 1:
        return [list(range(len(semantics)))]
    groups = []
    for position, column in enumerate(test_columns):
        start = 0 if position == 0 else column
        end = (
            test_columns[position + 1]
            if position + 1 < len(test_columns)
            else len(semantics)
        )
        groups.append(list(range(start, end)))
    return groups


# --------------------------------------------------------------------------- #
# Row guards
# --------------------------------------------------------------------------- #


def _row_is_metadata(row: list[Span]) -> bool:
    joined = " ".join(span.text for span in row).strip()
    if not joined:
        return True
    for pattern in ROW_METADATA_PATTERNS:
        if pattern.search(joined):
            return True
    return False


def _row_is_section(row: list[Span]) -> bool:
    cells = [span.text.strip() for span in row if span.text.strip()]
    if len(cells) > 2:
        return False
    has_number = any(any(ch.isdigit() for ch in cell) for cell in cells)
    if has_number:
        return False
    first = _norm(cells[0]) if cells else ""
    return first in SECTION_CUES or any(_norm(c) in SECTION_CUES for c in cells)


def _row_is_continuation(row: list[Span], previous_has_reference: bool) -> bool:
    """Reference-range continuation line (wrapped multi-line ranges)."""
    cells = [span.text.strip() for span in row if span.text.strip()]
    if not cells:
        return False
    if any(_is_unit(c) for c in cells):
        return False
    joined = " ".join(cells)
    if DECIMAL_RE.match(cells[0]) and len(cells) >= 2:
        return False
    return bool(CONTINUATION_CUES.match(joined)) or (
        previous_has_reference and RANGE_RE.search(joined)
    )


# --------------------------------------------------------------------------- #
# Row -> fields assembly
# --------------------------------------------------------------------------- #


def _positional_semantics(row: list[Span]) -> list[str]:
    """Best-effort column semantics when no header row is detected."""
    n = len(row)
    if n >= 4:
        return ["TEST", "RESULT", "UNIT", "REFERENCE"] + ["REFERENCE"] * (n - 4)
    if n == 3:
        third = row[2].text.strip()
        if _is_unit_cell(third):
            return ["TEST", "RESULT", "UNIT"]
        return ["TEST", "RESULT", "REFERENCE"]
    if n == 2:
        second = row[1].text.strip()
        if _is_unit_cell(second):
            return ["TEST", "UNIT"]
        return ["TEST", "RESULT"]
    return ["TEST"]


def _confidence(row: list[Span], has_header: bool) -> float:
    confidences = [span.confidence for span in row]
    ocr = sum(confidences) / len(confidences) if confidences else 0.0
    structure = 1.0 if has_header else 0.85
    return max(0.0, min(1.0, ocr * structure))


def _assemble(
    page_number: int,
    row_index: int,
    cells: list[tuple[Span, str]],
    has_header: bool,
) -> ParsedResult | None:
    """Assemble a structured row from (span, semantics) cells."""
    evidence = tuple(span.sequence for span, _ in cells)
    test_parts: list[str] = []
    result_candidates: list[str] = []
    result_text_parts: list[str] = []
    unit_candidates: list[str] = []
    reference_parts: list[str] = []
    reference_low: list[Decimal] = []
    reference_high: list[Decimal] = []
    flag_parts: list[str] = []
    seen_result = False

    for span, semantics in cells:
        stripped = span.text.strip()
        if not stripped:
            continue
        if semantics == "TEST":
            test_parts.append(stripped)
            continue
        # printed flags (H/L/R/*) take priority over unit shape detection:
        # a lone "H" between test and result is a flag, not a unit.
        if _is_flag(stripped):
            flag_parts.append(stripped)
            continue
        lowered = stripped.lower()
        if semantics == "RESULT":
            seen_result = True
            if DECIMAL_RE.match(lowered):
                result_candidates.append(stripped)
            elif (
                THRESHOLD_RE.match(stripped)
                or lowered in SPECIAL_RESULT
                or lowered.startswith(("negative", "positive", "not detected", "detected"))
            ):
                result_candidates.append(stripped)
                result_text_parts.append(stripped)
            elif PHONE_LIKE_RE.match(stripped):
                # phone numbers / big IDs are not lab values
                reference_parts.append(stripped)
            elif " " in stripped:
                # multi-word prose in the result column is not a lab value
                reference_parts.append(stripped)
            else:
                result_candidates.append(stripped)
                result_text_parts.append(stripped)
            continue
        if semantics == "UNIT":
            # A cell in the unit column must actually look like a unit.
            # Diagonal watermark strokes that land on the unit column (e.g. an
            # Arabic lab watermark) are dropped, never accepted as units;
            # OCR-degraded Latin cell-count units are preserved.
            if _is_unit_cell(stripped):
                unit_candidates.append(stripped)
                seen_result = True
            continue
        if not seen_result and _is_unit_cell(stripped):
            unit_candidates.append(stripped)
            seen_result = True
            continue
        if semantics in {"REF_LOW", "REF_HIGH"}:
            decimal = _to_decimal(stripped)
            if semantics == "REF_LOW" and decimal is not None:
                reference_low.append(decimal)
            elif semantics == "REF_HIGH" and decimal is not None:
                reference_high.append(decimal)
            reference_parts.append(stripped)
            continue
        if semantics == "REFERENCE" or RANGE_RE.match(stripped) or THRESHOLD_RE.match(stripped):
            reference_parts.append(stripped)
            range_match = RANGE_RE.match(stripped)
            if range_match:
                low = _to_decimal(range_match.group(2))
                high = _to_decimal(range_match.group(4))
                if low is not None and high is not None and low <= high:
                    reference_low.append(low)
                    reference_high.append(high)
            continue
        if DECIMAL_RE.match(lowered):
            if not seen_result:
                seen_result = True
                result_candidates.append(stripped)
            else:
                reference_parts.append(stripped)
            continue
        if lowered in SPECIAL_RESULT or lowered.startswith(("negative", "positive")):
            seen_result = True
            result_candidates.append(stripped)
            result_text_parts.append(stripped)
            continue
        # fallback: unmatched cell -> test-name continuation or reference
        if not seen_result and not test_parts:
            test_parts.append(stripped)
        else:
            reference_parts.append(stripped)

    test_name_raw = " ".join(test_parts).strip()
    if not test_name_raw:
        return None

    result_raw = " ".join(result_candidates).strip()
    unit_raw = " ".join(unit_candidates).strip()
    reference_raw = " ".join(reference_parts).strip()
    flag_raw = " ".join(flag_parts).strip()

    # A lab result must carry a value. Rows that are only prose/credentials/
    # metadata (doctor names, certifications, checklists) never qualify even if
    # a stray token looked unit- or reference-like.
    has_lab_evidence = bool(result_raw or result_text_parts)
    if not has_lab_evidence:
        return None

    numeric = None
    if result_raw:
        if DECIMAL_RE.match(result_raw):
            numeric = _to_decimal(result_raw)
        elif " " in result_raw:
            first = result_raw.split()[0]
            if DECIMAL_RE.match(first):
                numeric = _to_decimal(first)

    return ParsedResult(
        page_number=page_number,
        row_index=row_index,
        test_name_raw=test_name_raw,
        result_raw=result_raw,
        result_numeric=numeric,
        result_text=" ".join(result_text_parts).strip(),
        unit_raw=unit_raw,
        reference_range_raw=reference_raw,
        reference_low=reference_low[0] if len(reference_low) == 1 else None,
        reference_high=reference_high[0] if len(reference_high) == 1 else None,
        flag_raw=flag_raw,
        extraction_confidence=_confidence([span for span, _ in cells], has_header),
        evidence_sequence=evidence,
    )


# --------------------------------------------------------------------------- #
# Page parser
# --------------------------------------------------------------------------- #


def parse_page(
    page_number: int,
    spans: list[Span],
    *,
    tolerance_factor: float = 0.6,
) -> list[ParsedResult]:
    """Parse one page of normalized spans into structured lab results."""
    if not spans:
        return []
    rows = group_rows(spans, tolerance_factor=tolerance_factor)
    header_index = find_header_row(rows)
    has_header = header_index is not None

    if has_header:
        header_row = rows[header_index]
        bounds = _column_bounds(header_row)
        semantics = _column_semantics(header_row)
        groups = _split_header_groups(semantics)
    else:
        bounds = []
        semantics = []
        groups = []

    results: list[ParsedResult] = []
    row_index = 0
    # Reference continuation state: text fragments from a multi-line reference
    # range that should be merged into the most recently emitted result row.
    pending_reference: str | None = None
    pending_low: Decimal | None = None
    pending_high: Decimal | None = None
    last_result_index: int | None = None

    for index, row in enumerate(rows):
        if has_header and index == header_index:
            continue
        if has_header and index < header_index:
            # prelude rows (doctors, patient info, certifications) above the
            # table header are metadata, not lab rows
            continue
        if _header_score(row) >= 2:
            # repeated header row inside a multi-table page
            continue
        if _row_is_metadata(row):
            pending_reference = None
            continue
        if _row_is_section(row):
            continue

        # Drop diagonal watermark / stamp strokes before column assignment so
        # they can never contaminate unit/result/reference fields.
        row = _drop_geometric_noise(row)
        if not row:
            continue

        if not has_header:
            positional = _positional_semantics(row)
            cell_groups = [
                [
                    (span, positional[cell_index] if cell_index < len(positional) else "OTHER")
                    for cell_index, span in enumerate(row)
                ]
            ]
        else:
            above_header = index < header_index
            if above_header:
                # prelude rows (doctors, patient info) use positional fallback
                positional = _positional_semantics(row)
                cell_groups = [
                    [
                        (span, positional[cell_index] if cell_index < len(positional) else "OTHER")
                        for cell_index, span in enumerate(row)
                    ]
                ]
            else:
                cell_groups = []
                for group in groups:
                    group_cells = []
                    for span in row:
                        cx, _ = _center(span)
                        col_index = _assign_column(cx, bounds)
                        if col_index not in group:
                            continue
                        col_sem = (
                            semantics[col_index]
                            if 0 <= col_index < len(semantics)
                            else "OTHER"
                        )
                        group_cells.append((span, col_sem))
                    cell_groups.append(group_cells)

        if pending_reference is not None and _row_is_continuation(
            row, previous_has_reference=True
        ):
            extra = " ".join(span.text.strip() for span in row).strip()
            if extra and last_result_index is not None:
                current = results[last_result_index]
                merged = f"{current.reference_range_raw} {extra}".strip()
                results[last_result_index] = ParsedResult(
                    **{
                        **current.__dict__,
                        "reference_range_raw": merged,
                        "reference_low": current.reference_low or pending_low,
                        "reference_high": current.reference_high or pending_high,
                    }
                )
            continue

        for group_cells in cell_groups:
            if not group_cells:
                continue
            parsed = _assemble(
                page_number,
                row_index,
                group_cells,
                has_header,
            )
            if parsed is None:
                continue
            if parsed.test_name_raw.strip().lower() in KNOWN_UNITS:
                # bare known unit as a test name (chart axis fragment, not a test)
                continue
            results.append(parsed)
            last_result_index = len(results) - 1
            row_index += 1
            # Seed reference-continuation state only when this row ends on a
            # range (only meaningful for single-group rows).
            if (
                parsed.reference_range_raw
                and len(cell_groups) == 1
                and RANGE_ANYWHERE_RE.search(parsed.reference_range_raw)
            ):
                pending_reference = parsed.reference_range_raw
                pending_low = parsed.reference_low
                pending_high = parsed.reference_high
            else:
                pending_reference = None
                pending_low = None
                pending_high = None

    return results
