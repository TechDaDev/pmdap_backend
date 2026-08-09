import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum


class CandidateType(StrEnum):
    REPORT_DATE = "REPORT_DATE"
    RESULT_DATE = "RESULT_DATE"
    ISSUE_DATE = "ISSUE_DATE"
    COLLECTION_DATE = "COLLECTION_DATE"
    SAMPLE_DATE = "SAMPLE_DATE"
    EXAMINATION_DATE = "EXAMINATION_DATE"
    ADMISSION_DATE = "ADMISSION_DATE"
    DISCHARGE_DATE = "DISCHARGE_DATE"
    APPLICATION_DATE = "APPLICATION_DATE"
    PRINT_DATE = "PRINT_DATE"
    DATE_OF_BIRTH = "DATE_OF_BIRTH"
    UNKNOWN = "UNKNOWN"


DATE_PIPELINE_VERSION = "m9-date-v2"
DEFAULT_CONTEXT_MAX_CHARS = 160
DEFAULT_SUGGESTION_MIN_SCORE = 0.75
DEFAULT_SUGGESTION_TIE_TOLERANCE = 0.01
DEFAULT_FUTURE_TOLERANCE_DAYS = 14

DIGIT_TRANSLATION = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
SEPARATOR_TRANSLATION = str.maketrans(
    {
        "⁄": "/",
        "∕": "/",
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
    }
)

MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
MONTH_PATTERN = "|".join(sorted(MONTHS, key=len, reverse=True))

DATE_PATTERNS = (
    (
        "YMD_NUMERIC",
        re.compile(
            r"(?<!\d)(?P<year>\d{4})[-/.](?P<month>\d{1,2})[-/.](?P<day>\d{1,2})(?!\d)"
        ),
    ),
    (
        "DMY_NUMERIC",
        re.compile(
            r"(?<!\d)(?P<day>\d{1,2})[-/.](?P<month>\d{1,2})[-/.](?P<year>\d{4})(?!\d)"
        ),
    ),
    (
        "DMY_NAMED_MONTH",
        re.compile(
            rf"(?<!\w)(?P<day>\d{{1,2}})\s+(?P<month>{MONTH_PATTERN})\.?\s+(?P<year>\d{{4}})(?!\d)",
            re.IGNORECASE,
        ),
    ),
    (
        "MDY_NAMED_MONTH",
        re.compile(
            rf"(?<!\w)(?P<month>{MONTH_PATTERN})\.?\s+(?P<day>\d{{1,2}}),?\s+(?P<year>\d{{4}})(?!\d)",
            re.IGNORECASE,
        ),
    ),
)

LABELS = {
    CandidateType.REPORT_DATE: (
        "date of report",
        "report date",
        "تاريخ التقرير",
        "date",
        "التاريخ",
        "تاريخ",
    ),
    CandidateType.RESULT_DATE: ("result date", "تاريخ النتيجة"),
    CandidateType.ISSUE_DATE: (
        "issued date",
        "issue date",
        "issued",
        "تاريخ الإصدار",
        "تاريخ الاصدار",
    ),
    CandidateType.COLLECTION_DATE: (
        "collection date",
        "collected",
        "تاريخ الجمع",
    ),
    CandidateType.SAMPLE_DATE: ("sample date", "تاريخ العينة"),
    CandidateType.EXAMINATION_DATE: (
        "examination date",
        "exam date",
        "تاريخ الفحص",
    ),
    CandidateType.ADMISSION_DATE: ("admission date", "تاريخ الدخول"),
    CandidateType.DISCHARGE_DATE: ("discharge date", "تاريخ الخروج"),
    CandidateType.APPLICATION_DATE: (
        "date of application",
        "application date",
        "تاريخ التقديم",
    ),
    CandidateType.PRINT_DATE: ("print date", "printed", "تاريخ الطباعة"),
    CandidateType.DATE_OF_BIRTH: (
        "date of birth",
        "birth date",
        "birthday",
        "dob",
        "تاريخ الميلاد",
    ),
}
GENERIC_LABELS = frozenset({"date", "تاريخ", "التاريخ"})
LABEL_PROXIMITY_MAX_CHARS = 64
CLOSE_LABEL_MAX_CHARS = 16
SAME_LINE_LABEL_BONUS = 0.04
CLOSE_LABEL_BONUS = 0.04
ADJACENT_LINE_LABEL_BONUS = 0.03
GENERIC_LABEL_PENALTY = 0.15
AMBIGUOUS_NUMERIC_PENALTY = 0.15
FUTURE_DATE_PENALTY = 0.55
COMPACT_DATE_PENALTY = 0.10
TYPE_BASE_SCORES = {
    CandidateType.REPORT_DATE: 0.90,
    CandidateType.RESULT_DATE: 0.87,
    CandidateType.ISSUE_DATE: 0.84,
    CandidateType.EXAMINATION_DATE: 0.82,
    CandidateType.COLLECTION_DATE: 0.70,
    CandidateType.SAMPLE_DATE: 0.68,
    CandidateType.DISCHARGE_DATE: 0.65,
    CandidateType.ADMISSION_DATE: 0.60,
    CandidateType.APPLICATION_DATE: 0.50,
    CandidateType.PRINT_DATE: 0.30,
    CandidateType.DATE_OF_BIRTH: 0.05,
    CandidateType.UNKNOWN: 0.20,
}

# Strict compact-date recovery: exactly eight digits under a strong explicit
# date label, never near identifier semantics (M16.1 §24-25).
COMPACT_DATE_PATTERN = re.compile(r"(?<!\d)(\d{8})(?!\d)")
IDENTIFIER_HINTS = (
    "number",
    "barcode",
    "serial",
    "رقم",
    "patient number",
    "national",
    "code",
    "identifier",
)


@dataclass(frozen=True)
class NormalizedText:
    text: str
    raw_text: str
    source_indices: tuple[int, ...]

    def raw_slice(self, start: int, end: int) -> str:
        if start < 0 or end > len(self.source_indices) or start >= end:
            return ""
        return self.raw_text[
            self.source_indices[start] : self.source_indices[end - 1] + 1
        ]


@dataclass(frozen=True)
class DateCandidateData:
    detected_date: date
    alternative_date: date | None
    raw_value: str
    normalized_value: str
    candidate_type: CandidateType
    score: float
    page_number: int
    context: str
    source: str
    occurrence_index: int
    ambiguous: bool
    parsing_rule: str
    pipeline_version: str = DATE_PIPELINE_VERSION


@dataclass(frozen=True)
class _Classification:
    candidate_type: CandidateType
    distance: int | None
    generic: bool
    line_relation: str = "same"  # "same" | "adjacent"


def normalize_text(raw_text: str) -> NormalizedText:
    expanded = []
    indices = []
    for index, character in enumerate(raw_text):
        for normalized in unicodedata.normalize("NFKC", character):
            normalized = normalized.translate(DIGIT_TRANSLATION).translate(
                SEPARATOR_TRANSLATION
            )
            if unicodedata.category(normalized) in {"Cc", "Cf"} and not (
                normalized.isspace()
            ):
                continue
            expanded.append(normalized)
            indices.append(index)

    output = []
    output_indices = []
    position = 0
    while position < len(expanded):
        character = expanded[position]
        if not character.isspace():
            output.append(character)
            output_indices.append(indices[position])
            position += 1
            continue
        whitespace_start = position
        contains_newline = False
        while position < len(expanded) and expanded[position].isspace():
            contains_newline = contains_newline or expanded[position] in "\r\n"
            position += 1
        output.append("\n" if contains_newline else " ")
        output_indices.append(indices[whitespace_start])
    return NormalizedText("".join(output), raw_text, tuple(output_indices))


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_match(match, rule):
    month_value = match.group("month").casefold().rstrip(".")
    month = MONTHS.get(month_value, int(month_value) if month_value.isdigit() else 0)
    parsed = _safe_date(int(match.group("year")), month, int(match.group("day")))
    if parsed is None:
        return None
    alternative = None
    ambiguous = False
    if rule == "DMY_NUMERIC":
        day = int(match.group("day"))
        numeric_month = int(match.group("month"))
        if day <= 12 and numeric_month <= 12 and day != numeric_month:
            alternative = _safe_date(int(match.group("year")), day, numeric_month)
            ambiguous = alternative is not None and alternative != parsed
    return parsed, alternative, ambiguous


def _label_matches(line: str):
    folded = line.casefold()
    for candidate_type, labels in LABELS.items():
        for label in labels:
            pattern = re.compile(rf"(?<!\w){re.escape(label.casefold())}(?!\w)")
            for match in pattern.finditer(folded):
                yield (
                    candidate_type,
                    label,
                    match.start(),
                    match.end(),
                    label in GENERIC_LABELS,
                )


def _classify_line(line: str, date_start: int, date_end: int) -> _Classification | None:
    """Proximity-based classification on one line; None if no label near date."""
    matches = []
    for (
        candidate_type,
        label,
        label_start,
        label_end,
        generic,
    ) in _label_matches(line):
        if label_end <= date_start:
            distance = date_start - label_end
        elif label_start >= date_end:
            distance = label_start - date_end
        else:
            distance = 0
        if distance <= LABEL_PROXIMITY_MAX_CHARS:
            matches.append(
                (
                    distance,
                    -len(label),
                    candidate_type.value,
                    candidate_type,
                    label,
                    generic,
                )
            )
    if not matches:
        return None
    distance, _, _, candidate_type, label, generic = min(matches)
    return _Classification(candidate_type, distance, generic, "same")


def _adjacent_previous_line(text: str, before_line_start: int):
    """Previous non-empty line, allowing at most one empty line in between."""
    prev_end = before_line_start - 1
    if prev_end < 0:
        return None
    prev_start = text.rfind("\n", 0, prev_end) + 1
    prev_line = text[prev_start:prev_end]
    if prev_line.strip():
        return (prev_start, prev_end)
    prev_end2 = prev_start - 1
    if prev_end2 < 0:
        return None
    prev_start2 = text.rfind("\n", 0, prev_end2) + 1
    prev_line2 = text[prev_start2:prev_end2]
    if prev_line2.strip():
        return (prev_start2, prev_end2)
    return None


def _classify_adjacent_line(line: str) -> _Classification | None:
    """Best label anywhere in an adjacent line; None if the line has no label."""
    labels = list(_label_matches(line))
    if not labels:
        return None
    candidate_type, label, _, _, generic = max(
        labels, key=lambda item: (len(item[1]), -item[2])
    )
    return _Classification(candidate_type, None, generic, "adjacent")


def _classify(text: str, date_start: int, date_end: int) -> _Classification:
    """Classify a date occurrence using same-line then adjacent-line labels.

    Hierarchy (M16.1 §17): same-line explicit label > adjacent-line explicit
    label > generic nearby evidence > unlabeled (UNKNOWN).

    Adjacent-line association is deliberately restricted to the immediately
    preceding line (label-before-value reading order). The following line is
    not consulted: a trailing generic/other-field label must not reassign a
    date (avoids wrong confident suggestions, M16.1 §36).
    """
    line_start = text.rfind("\n", 0, date_start) + 1
    line_end = text.find("\n", date_end)
    if line_end < 0:
        line_end = len(text)
    current = text[line_start:line_end]
    result = _classify_line(current, date_start - line_start, date_end - line_start)
    if result is not None:
        return result
    previous = _adjacent_previous_line(text, line_start)
    if previous is not None:
        prev_start, prev_end = previous
        adjacent = _classify_adjacent_line(text[prev_start:prev_end])
        if adjacent is not None:
            return adjacent
    return _Classification(CandidateType.UNKNOWN, None, False, "same")


def _score(
    classification: _Classification,
    *,
    ambiguous: bool,
    detected_date: date,
    today: date,
    future_tolerance_days: int,
) -> float:
    score = TYPE_BASE_SCORES[classification.candidate_type]
    if classification.line_relation == "same":
        if classification.distance is not None:
            score += SAME_LINE_LABEL_BONUS
            if classification.distance <= CLOSE_LABEL_MAX_CHARS:
                score += CLOSE_LABEL_BONUS
    elif classification.line_relation == "adjacent":
        score += ADJACENT_LINE_LABEL_BONUS
    if classification.generic:
        score -= GENERIC_LABEL_PENALTY
    if ambiguous:
        score -= AMBIGUOUS_NUMERIC_PENALTY
    if detected_date > today + timedelta(days=future_tolerance_days):
        score -= FUTURE_DATE_PENALTY
    return round(min(1.0, max(0.0, score)), 4)


def _bounded_context(text: str, start: int, end: int, maximum: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    context = text[line_start:line_end].strip()
    if len(context) <= maximum:
        return context
    match_start = start - line_start
    match_end = end - line_start
    left = max(0, match_start - (maximum - (match_end - match_start)) // 2)
    right = min(len(context), left + maximum)
    left = max(0, right - maximum)
    return context[left:right]


def detect_page_dates(
    raw_text: str,
    *,
    page_number: int,
    source: str,
    today: date | None = None,
    context_max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
    future_tolerance_days: int = DEFAULT_FUTURE_TOLERANCE_DAYS,
) -> tuple[DateCandidateData, ...]:
    if source not in {"PDF_TEXT", "OCR"}:
        raise ValueError("Date candidate source must be PDF_TEXT or OCR.")
    normalized = normalize_text(raw_text)
    occurrences = []
    occupied = []
    for rule, pattern in DATE_PATTERNS:
        for match in pattern.finditer(normalized.text):
            if any(
                match.start() < end and match.end() > start for start, end in occupied
            ):
                continue
            parsed = _parse_match(match, rule)
            if parsed is None:
                continue
            occupied.append((match.start(), match.end()))
            occurrences.append((match.start(), match.end(), rule, match, parsed))

    candidates = []
    current_date = today or date.today()
    for start, end, rule, match, parsed in sorted(occurrences):
        classification = _classify(normalized.text, start, end)
        detected_date, alternative_date, ambiguous = parsed
        candidates.append(
            DateCandidateData(
                detected_date=detected_date,
                alternative_date=alternative_date,
                raw_value=normalized.raw_slice(start, end),
                normalized_value=match.group(0),
                candidate_type=classification.candidate_type,
                score=_score(
                    classification,
                    ambiguous=ambiguous,
                    detected_date=detected_date,
                    today=current_date,
                    future_tolerance_days=future_tolerance_days,
                ),
                page_number=page_number,
                context=_bounded_context(
                    normalized.text, start, end, context_max_chars
                ),
                source=source,
                occurrence_index=start,
                ambiguous=ambiguous,
                parsing_rule=rule,
            )
        )

    # Compact eight-digit date recovery, strictly gated (M16.1 §24-25).
    for match in COMPACT_DATE_PATTERN.finditer(normalized.text):
        start, end = match.span()
        if any(start < e and end > s for s, e in occupied):
            continue
        digits = match.group(1)
        if digits[:2] == "00" or digits[2:4] == "00":
            continue
        year = int(digits[4:8])
        if not 1900 <= year <= 2100:
            continue
        parsed = _safe_date(year, int(digits[2:4]), int(digits[:2]))
        if parsed is None:
            continue
        classification = _classify(normalized.text, start, end)
        if classification.candidate_type in (
            CandidateType.UNKNOWN,
            CandidateType.DATE_OF_BIRTH,
        ):
            continue
        if classification.generic:
            continue
        if parsed > current_date + timedelta(days=future_tolerance_days):
            continue
        line_start = normalized.text.rfind("\n", 0, start) + 1
        line_end = normalized.text.find("\n", end)
        if line_end < 0:
            line_end = len(normalized.text)
        folded = normalized.text[line_start:line_end].casefold()
        if any(hint in folded for hint in IDENTIFIER_HINTS):
            continue
        occupied.append((start, end))
        compact_score = _score(
            classification,
            ambiguous=False,
            detected_date=parsed,
            today=current_date,
            future_tolerance_days=future_tolerance_days,
        )
        candidates.append(
            DateCandidateData(
                detected_date=parsed,
                alternative_date=None,
                raw_value=normalized.raw_slice(start, end),
                normalized_value=digits,
                candidate_type=classification.candidate_type,
                score=round(max(0.0, compact_score - COMPACT_DATE_PENALTY), 4),
                page_number=page_number,
                context=_bounded_context(
                    normalized.text, start, end, context_max_chars
                ),
                source=source,
                occurrence_index=start,
                ambiguous=False,
                parsing_rule="COMPACT_DMY",
            )
        )
    return tuple(sorted(candidates, key=lambda c: c.occurrence_index))


def choose_suggested_index(
    candidates: tuple[DateCandidateData, ...] | list[DateCandidateData],
    *,
    minimum_score: float = DEFAULT_SUGGESTION_MIN_SCORE,
    tie_tolerance: float = DEFAULT_SUGGESTION_TIE_TOLERANCE,
) -> int | None:
    if not candidates:
        return None
    ranked = sorted(
        enumerate(candidates),
        key=lambda item: (
            -item[1].score,
            item[1].page_number,
            item[1].occurrence_index,
        ),
    )
    best_index, best = ranked[0]
    if best.score < minimum_score:
        return None
    if len(ranked) > 1:
        second = ranked[1][1]
        if (
            abs(best.score - second.score) <= tie_tolerance
            and best.detected_date != second.detected_date
        ):
            return None
    return best_index
