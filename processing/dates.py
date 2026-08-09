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
    PRINT_DATE = "PRINT_DATE"
    DATE_OF_BIRTH = "DATE_OF_BIRTH"
    UNKNOWN = "UNKNOWN"


DATE_PIPELINE_VERSION = "m9-date-v1"
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
    CandidateType.PRINT_DATE: ("print date", "printed", "تاريخ الطباعة"),
    CandidateType.DATE_OF_BIRTH: (
        "date of birth",
        "birth date",
        "dob",
        "تاريخ الميلاد",
    ),
}
GENERIC_LABELS = frozenset({"date", "تاريخ", "التاريخ"})
LABEL_PROXIMITY_MAX_CHARS = 64
CLOSE_LABEL_MAX_CHARS = 16
SAME_LINE_LABEL_BONUS = 0.04
CLOSE_LABEL_BONUS = 0.04
GENERIC_LABEL_PENALTY = 0.25
AMBIGUOUS_NUMERIC_PENALTY = 0.15
FUTURE_DATE_PENALTY = 0.55
TYPE_BASE_SCORES = {
    CandidateType.REPORT_DATE: 0.90,
    CandidateType.RESULT_DATE: 0.87,
    CandidateType.ISSUE_DATE: 0.84,
    CandidateType.EXAMINATION_DATE: 0.82,
    CandidateType.COLLECTION_DATE: 0.70,
    CandidateType.SAMPLE_DATE: 0.68,
    CandidateType.DISCHARGE_DATE: 0.65,
    CandidateType.ADMISSION_DATE: 0.60,
    CandidateType.PRINT_DATE: 0.30,
    CandidateType.DATE_OF_BIRTH: 0.05,
    CandidateType.UNKNOWN: 0.20,
}


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
                yield candidate_type, label, match.start(), match.end()


def _classify(line: str, date_start: int, date_end: int) -> _Classification:
    matches = []
    for candidate_type, label, label_start, label_end in _label_matches(line):
        if label_end <= date_start:
            distance = date_start - label_end
        elif label_start >= date_end:
            distance = label_start - date_end
        else:
            distance = 0
        if distance <= LABEL_PROXIMITY_MAX_CHARS:
            matches.append(
                (distance, -len(label), candidate_type.value, candidate_type, label)
            )
    if not matches:
        return _Classification(CandidateType.UNKNOWN, None, False)
    distance, _, _, candidate_type, label = min(matches)
    return _Classification(candidate_type, distance, label in GENERIC_LABELS)


def _score(
    classification: _Classification,
    *,
    ambiguous: bool,
    detected_date: date,
    today: date,
    future_tolerance_days: int,
) -> float:
    score = TYPE_BASE_SCORES[classification.candidate_type]
    if classification.distance is not None:
        score += SAME_LINE_LABEL_BONUS
        if classification.distance <= CLOSE_LABEL_MAX_CHARS:
            score += CLOSE_LABEL_BONUS
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
        line_start = normalized.text.rfind("\n", 0, start) + 1
        line_end = normalized.text.find("\n", end)
        if line_end < 0:
            line_end = len(normalized.text)
        classification = _classify(
            normalized.text[line_start:line_end], start - line_start, end - line_start
        )
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
    return tuple(candidates)


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
