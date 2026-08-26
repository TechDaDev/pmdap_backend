"""Advisory identity field extraction.

Deterministic only: OCR (PaddleOCR when installed) + a pure ICAO MRZ parser +
label-anchor heuristics. No LLM, no cloud OCR, no medical interpretation.

Extraction NEVER persists an IdentityDocument and NEVER returns raw OCR text.
"""
from __future__ import annotations

import logging
import os
import re
import threading

from django.utils import timezone

from identities import mrz

logger = logging.getLogger(__name__)

# Response contract version.
EXTRACTOR_VERSION = "identity-v1"

# Source values used in the response contract.
SRC_MRZ = "MRZ"
SRC_OCR = "OCR"
SRC_DOCUMENT_TYPE = "DOCUMENT_TYPE"
SRC_DERIVED = "DERIVED"

# Warning codes.
W_MRZ_CHECK_FAILED = "MRZ_CHECK_FAILED"
W_FIELD_NOT_FOUND = "FIELD_NOT_FOUND"
W_LOW_CONFIDENCE = "LOW_CONFIDENCE"
W_UNSUPPORTED_LAYOUT = "UNSUPPORTED_LAYOUT"
W_NO_TEXT = "NO_TEXT_DETECTED"
W_MRZ_NOT_DETECTED = "MRZ_NOT_DETECTED"
W_OCR_UNAVAILABLE = "OCR_UNAVAILABLE"

_ocr_lock = threading.Lock()
_ocr = None
_ocr_attempted = False


def _load_ocr():
    """Lazy singleton. Returns a callable or None if OCR is unavailable.

    Kept behind a lock; a failed load degrades to None (endpoint still returns
    a structured response, just without extracted values).
    """
    global _ocr, _ocr_attempted
    with _ocr_lock:
        if _ocr_attempted:
            return _ocr
        _ocr_attempted = True
        try:
            # PaddlePaddle 3.x CPU inference crashes through the oneDNN/PIR
            # path on this stack (ConvertPirAttribute2RuntimeAttribute); force
            # the reference (non-MKLDNN) path.
            os.environ.setdefault("FLAGS_use_mkldnn", "0")
            from paddleocr import PaddleOCR  # type: ignore

            _ocr = PaddleOCR(lang="en", enable_mkldnn=False)
        except Exception as exc:  # pragma: no cover - env dependent
            logger.warning("identity OCR unavailable: %s", type(exc).__name__)
            _ocr = None
        return _ocr


def ocr_text(image_path: str) -> list[str]:
    """Run OCR; returns a flat list of text lines (best-effort)."""
    engine = _load_ocr()
    if engine is None:
        return []
    try:
        # PaddleOCR 3.x: `predict()` returns PaddleX result objects that carry
        # a `rec_texts` list (the legacy `ocr(..., cls=True)` API is removed).
        results = engine.predict(image_path)
        lines: list[str] = []
        for result in results or []:
            if isinstance(result, dict):
                texts = result.get("rec_texts")
            else:
                texts = getattr(result, "rec_texts", None)
            for text in texts or []:
                text = str(text).strip()
                if text:
                    lines.append(text)
        return lines
    except Exception:  # pragma: no cover - engine failure path
        logger.warning("identity OCR inference failed", exc_info=True)
        return []


_DATE_RE = re.compile(r"\b((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b")
_ISSUE_LABELS = ("issue date", "date of issue", "issued on", "date issued")
_MRZ_LINES = re.compile(r"^[A-Z0-9<]{30,44}$")

# --- Iraqi National Card profile-field extraction ---------------------------
# Additive sources for the card path; the public serializer keeps the original
# MRZ/OCR/DOCUMENT_TYPE/DERIVED choices and gains these new ones.
SRC_FRONT = "FRONT_PRINTED"
SRC_BACK = "BACK_PRINTED"
SRC_ROI = "ROI"

# Additive warning codes for the card path.
W_SOURCE_MISMATCH = "SOURCE_MISMATCH"
W_OCR_NORMALIZED = "OCR_CHARACTER_NORMALIZED"
W_FAMILY_NOT_FOUND = "FAMILY_NUMBER_NOT_FOUND"
W_BLOOD_GROUP_NOT_FOUND = "BLOOD_GROUP_NOT_FOUND"
W_FIELD_MISSING = "FIELD_MISSING"
W_MRZ_PARTIAL = "MRZ_PARTIAL"

_VALID_BLOOD_GROUPS = frozenset({"A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"})
# OCR-confusable letters mapped to digits ONLY where a numeric format is
# required (the card number). Never applied to names or the H... body number.
_CARD_CONFUSABLES = {"O": "0", "I": "1", "l": "1", "S": "5", "B": "8"}

# Arabic/Kurdish label anchors for the FRONT name section (matched on a
# lightly normalized form). Each group also carries "glued" prefixes that OCR
# can attach directly to the value with no space (e.g. "باوكاسماعيل").
# Priority order matters: mother switches the parser out of the paternal
# section, surname is ignored, and only the paternal grandfather counts.
_FRONT_NAME_GROUPS = [
    (
        "mother",
        ("الام", "ادايك", "ردايك", "داتك", "مادر"),
        ("ردايك", "ادايك", "داتك", "مادر"),
    ),
    (
        "surname",
        ("اللقب", "لقب", "انازناو", "نازناو"),
        ("انازناو", "نازناو"),
    ),
    (
        "grandfather",
        ("ابابير", "ابير", "الحد", "الجد", "باپير", "اباير"),
        ("ابابير", "ابير", "الحد", "الجد", "باپير", "اباير"),
    ),
    (
        "father",
        ("الاب", "اباوك", "باوك", "بابك", "بابه", "ابت", "باو"),
        ("اباوك", "باوك", "بابك", "بابه", "ابت", "باو"),
    ),
    (
        "name",
        ("الاسم", "اسم", "اناو", "ناو"),
        ("الاسم", "اناو"),
    ),
]
_NAME_LABELS = tuple(
    label for _, labels, _ in _FRONT_NAME_GROUPS for label in labels
)
_FATHER_LABELS = ("الاب", "اباوك", "باوك", "بابك", "بابه", "ابت", "باو")
_GRANDFATHER_LABELS = ("ابابير", "ابير", "الحد", "الجد", "باپير", "اباير")
_MOTHER_LABELS = ("الام", "ادايك", "ردايك", "داتك", "مادر")
_SEX_LABELS = ("الجنس", "اركمز", "اركز", "ارهگهز")
_NON_NAME_LABELS = _SEX_LABELS + (
    "الجنسية",
    "القومية",
    "فصيلة الدم",
    "زمرة الدم",
    "الرقم الوطني",
    "رقم البطاقة",
    "الرقم العائلي",
    "الرقم العائلى",
    "تاريخ الولادة",
    "تأريخ الولادة",
)
_FAMILY_LABELS = (
    "الرقم العائلي",
    "الرقم العائلى",
    "العائلي",
    "العائلى",
    "خيرائى",
    "خاني",
)
_CONNECTOR_TOKENS = frozenset({"اناو", "اتو", "ناو", "انازناو", "اركمز", "اركز"})

_HAMZA_MAP = str.maketrans("أإآٱ", "اااا")
_HARAKAT = "\u0640\u064B\u064C\u064D\u064E\u064F\u0650\u0651\u0652"
_HIDDEN = "\u200c\u200d\u200f\u200e"

_CARD_NUMBER_RE = re.compile(r"\b[0-9]{10,13}[A-Z0-9]?\b")
# Iraqi card body number: one valid uppercase Latin letter + 8 digits
# (observed H######## and G########; NOT restricted to H).
_BODY_NUMBER_RE = re.compile(r"\b[A-Z]\d{8}\b")
# No trailing \b: "+"/"-" are non-word chars, so a trailing boundary can never
# match at end-of-line. \b at the start keeps "1O+" / embedded letters out.
_BLOOD_RE = re.compile(r"\b([ABO]{1,2})\s*([+-])")
_FAMILY_RE = re.compile(r"[A-Z0-9]{10,}")


class SideLine:
    """OCR line tagged with its source side/region for card extraction.

    ``side`` is one of "FRONT" / "BACK" (full-card Arabic OCR) or an ROI tag
    such as "ROI_BLOOD" / "ROI_DATES" / "ROI_DOB" / "ROI_FAMILY" / "ROI_MRZ".
    """

    __slots__ = ("side", "text", "confidence")

    def __init__(self, side: str, text: str, confidence: float = 0.0):
        self.side = side
        self.text = text
        self.confidence = confidence


def _norm_ar(text: str) -> str:
    """Conservative Arabic normalization for LABEL matching (never values)."""
    text = text.translate(_HAMZA_MAP)
    for ch in _HARAKAT + _HIDDEN:
        text = text.replace(ch, "")
    return text


def _contains_any(line: str, labels) -> bool:
    norm = _norm_ar(line)
    return any(label in norm for label in labels)


def _value_after_label(text: str, labels) -> str:
    """Strip a leading label + glued connector prefix; keep the name value.

    Handles same-line ("الاسم اناو اسماعيل"), glued label+value
    ("باوكاسماعيل", "ابابيراسماعيل"), and connector-attached
    ("الاسم ناواسامه") OCR variants. Name labels are only dropped as whole
    leading tokens so legitimate names like "اسماعيل" are never mangled.
    """
    norm = _norm_ar(text)
    for sep in (":", "|", "/", "؛", ";", "—"):
        norm = norm.replace(sep, " ")
    tokens = norm.split()
    out: list[str] = []
    started = False
    for tok in tokens:
        t = tok
        if not started:
            if t in labels or t in _CONNECTOR_TOKENS:
                continue
            for pre in (
                *_glued_prefixes(labels),
                *sorted(_CONNECTOR_TOKENS, key=len, reverse=True),
            ):
                if t.startswith(pre) and len(t) > len(pre):
                    t = t[len(pre):]
                    if not t:
                        t = ""
                    break
            started = True
        if t:
            out.append(t)
    return " ".join(out)


def _glued_prefixes(labels) -> tuple[str, ...]:
    """Glued prefixes associated with a field's label set."""
    for _, group_labels, glued in _FRONT_NAME_GROUPS:
        if labels == group_labels or set(labels).issubset(set(group_labels)):
            return glued
    return ()


def _classify_front_line(text: str) -> str | None:
    """Classify a FRONT line into a name-section field (priority order).

    Matches whole-token labels OR a glued label prefix. Returns one of
    "mother" / "surname" / "grandfather" / "father" / "name" or None.
    """
    norm = _norm_ar(text)
    for sep in (":", "|", "/", "؛", ";", "—"):
        norm = norm.replace(sep, " ")
    tokens = norm.split()
    for field, labels, glued in _FRONT_NAME_GROUPS:
        for tok in tokens:
            if tok in labels:
                return field
            for pre in glued:
                if tok.startswith(pre) and len(tok) > len(pre):
                    return field
    return None


def _is_safe_name_value(text: str) -> bool:
    """Fail closed when a split label is followed by non-name card data."""
    value = _norm_ar(text).strip()
    if not value or any(char.isdigit() for char in value):
        return False
    if _contains_any(value, _NON_NAME_LABELS):
        return False
    if _classify_front_line(value) is not None:
        return False
    if _BLOOD_RE.search(value.upper()):
        return False
    return any(char.isalpha() for char in value)


def _match_front_field(front, labels):
    """Return (value, confidence) for the first line carrying a label.

    Uses the same label-aware value stripper as the name chain so glued and
    connector variants are handled consistently (used for sex).
    """
    for text, conf in front:
        if _contains_any(text, labels):
            value = _value_after_label(text, labels)
            if value:
                return value, conf
    return None, 0.0


def _parse_front_name_chain(front):
    """Deterministic front-card name-section parser (state machine).

    Processes ordered FRONT lines: NAME → FATHER → PATERNAL GRANDFATHER →
    SURNAME(ignored) → MOTHER. Same-line and split-line (label line then
    value line) variants are handled. Only the paternal grandfather before the
    mother section populates grandfather_name; the maternal grandfather is
    never captured. Once the mother section begins, the first mother-labeled
    value populates mother_name (deterministic maternal evidence for MOTHER
    guardian relationships).

    Returns (name, name_conf, father, father_conf, grandfather,
    grandfather_conf, mother, mother_conf).
    """
    name = father = grandfather = mother = None
    name_conf = father_conf = grandfather_conf = mother_conf = 0.0
    paternal_section = True
    pending = None  # (field, label_set) awaiting a split-line value

    def capture(field, value, conf):
        nonlocal name, father, grandfather, mother
        nonlocal name_conf, father_conf, grandfather_conf, mother_conf
        if field == "name" and name is None:
            name, name_conf = value, conf
        elif field == "father" and father is None:
            father, father_conf = value, conf
        elif field == "grandfather" and grandfather is None:
            grandfather, grandfather_conf = value, conf
        elif field == "mother" and mother is None and _is_safe_name_value(value):
            mother, mother_conf = value, conf

    for text, conf in front:
        if _contains_any(text, _MOTHER_LABELS):
            if not paternal_section:
                # A later mother line is the actual value line when the first
                # was only a label ("الام" alone). Capture it as mother only
                # if no mother value was seen yet.
                if mother is None:
                    value = _value_after_label(text, _MOTHER_LABELS)
                    if value:
                        capture("mother", value, conf)
                continue
            paternal_section = False
            pending = None
            value = _value_after_label(text, _MOTHER_LABELS)
            if value:
                capture("mother", value, conf)
            else:
                pending = ("mother", _MOTHER_LABELS)
            continue
        if not paternal_section:
            # Only a mother value line may follow the mother label; sex/other
            # labeled lines never become a mother name.
            if (
                pending is not None
                and pending[0] == "mother"
                and not _contains_any(text, _SEX_LABELS)
            ):
                value = _value_after_label(text, ())
                if value:
                    capture("mother", value, conf)
                pending = None
            continue
        field = _classify_front_line(text)
        if field is None:
            if pending is not None and not _contains_any(text, _SEX_LABELS):
                value = _value_after_label(text, ())
                if value:
                    capture(pending[0], value, conf)
                pending = None
            continue
        if field in ("surname", "mother"):
            continue
        value = _value_after_label(text, _labels_for(field))
        if value:
            capture(field, value, conf)
            pending = None
        else:
            pending = (field, _labels_for(field))
    return (
        name,
        name_conf,
        father,
        father_conf,
        grandfather,
        grandfather_conf,
        mother,
        mother_conf,
    )


def _labels_for(field: str):
    for f, labels, _ in _FRONT_NAME_GROUPS:
        if f == field:
            return labels
    return ()


def _clamp_conf(confidence: float, default: float = 0.7) -> float:
    if not confidence or confidence <= 0:
        return default
    return round(min(max(confidence, 0.0), 1.0), 3)


def _canonical_sex(value: str) -> str | None:
    norm = _norm_ar(value)
    if "ذكر" in norm:
        return "MALE"
    if any(t in norm for t in ("أنثى", "انثى", "انثئ")):
        return "FEMALE"
    return None


def _parse_date_str(text: str) -> str | None:
    m = _DATE_RE.search(text)
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if month < 1 or month > 12 or day < 1 or day > 31:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _extract_dates(lines) -> list[str]:
    out: list[str] = []
    for text, _ in lines:
        value = _parse_date_str(text)
        if value:
            out.append(value)
    return out


def _extract_dob(roi_dob) -> str | None:
    """Date of birth = oldest non-future date in the DOB ROI."""
    today = timezone.localdate().isoformat()
    past = [d for d in _extract_dates(roi_dob) if d <= today]
    return min(past) if past else None


def _extract_issue_expiry(roi_dates):
    today = timezone.localdate().isoformat()
    past, future = [], []
    for d in _extract_dates(roi_dates):
        (future if d > today else past).append(d)
    issue = max(past) if past else None
    expiry = min(future) if future else None
    return issue, expiry


def _extract_blood_group(roi_blood) -> str | None:
    for text, _ in roi_blood:
        candidate = text.replace("0", "O")
        m = _BLOOD_RE.search(candidate)
        if m:
            group = m.group(1).upper() + m.group(2)
            if group in _VALID_BLOOD_GROUPS:
                return group
    return None


def _normalize_card_confusables(value: str) -> tuple[str, bool]:
    out, changed = [], False
    for ch in value:
        if ch in _CARD_CONFUSABLES:
            out.append(_CARD_CONFUSABLES[ch])
            changed = True
        else:
            out.append(ch)
    return "".join(out), changed


def _extract_card_number(front):
    """Visible national/card number from the FRONT (numeric slot only).

    Returns (value, confidence, normalized). Confusable letters are rewritten
    to digits only when the resulting value becomes purely numeric.
    """
    for text, conf in front:
        for m in _CARD_NUMBER_RE.finditer(text.upper()):
            value = m.group(0)
            if value.startswith("H") or len(value) < 10:
                continue
            normed, changed = _normalize_card_confusables(value)
            if normed.isdigit():
                return normed, conf, changed
            # Non-confusable letters remain -> not a reliable numeric slot.
    return None, 0.0, False


def _is_body_number(value: str | None) -> bool:
    return bool(value) and bool(_BODY_NUMBER_RE.fullmatch(value))


def _extract_body_number(front) -> str | None:
    for text, _ in front:
        m = _BODY_NUMBER_RE.search(text.upper())
        if m:
            return m.group(0)
    return None


def _extract_family_number(roi_family, back, body_number):
    """Family number from the BACK printed region (long alphanumeric run).

    Only labeled/long-form candidates qualify. Short digit groups and MRZ
    noise are never accepted; MISSING is safer than WRONG. The H... body
    number is explicitly excluded.
    """
    candidates: list[str] = []
    for text, _ in roi_family:
        for m in _FAMILY_RE.finditer(text.upper()):
            cand = m.group(0)
            if "/" in cand or "-" in cand or cand == body_number:
                continue
            candidates.append(cand)
    if not candidates:
        for text, _ in back:
            if not _contains_any(text, _FAMILY_LABELS):
                continue
            for m in _FAMILY_RE.finditer(text.upper()):
                cand = m.group(0)
                if "/" in cand or "-" in cand or cand == body_number:
                    continue
                candidates.append(cand)
    if not candidates:
        return None
    return max(set(candidates), key=len)


def _mrz_lines(lines: list[str]) -> list[str]:
    return [l for l in lines if _MRZ_LINES.match(l)]


def _find_issue_date(lines: list[str]) -> tuple[str | None, float]:
    for line in lines:
        low = line.lower()
        if any(label in low for label in _ISSUE_LABELS):
            m = _DATE_RE.search(line)
            if m:
                return _date_from(m), 0.85
    # Fallback: any plausible date on the visual zone (lower confidence).
    for line in lines:
        m = _DATE_RE.search(line)
        if m:
            return _date_from(m), 0.6
    return None, 0.0


def _date_from(m: re.Match) -> str:
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _candidate(
    value: str | None,
    confidence: float,
    source: str,
    cross_check: str | None = None,
) -> dict:
    result = {
        "value": value,
        "confidence": round(min(max(confidence, 0.0), 1.0), 3),
        "source": source,
    }
    if cross_check:
        result["cross_check"] = cross_check
    return result


def extract_passport(lines: list[str]) -> tuple[dict, list[str]]:
    fields: dict[str, dict] = {}
    warnings: list[str] = []

    mrz_lines = _mrz_lines(lines)
    result = mrz.parse_mrz(mrz_lines) if mrz_lines else mrz.MrzResult()
    if result.detected:
        for key, value in mrz.mrz_to_fields(result).items():
            fields[key] = value
        if result.low_confidence_fields:
            warnings.append(W_MRZ_CHECK_FAILED)
    elif lines:
        warnings.append(W_MRZ_NOT_DETECTED)

    # Issue date is not on standard MRZ — visual-zone OCR.
    issue_date, conf = _find_issue_date(lines)
    if issue_date:
        fields["issue_date"] = _candidate(issue_date, conf, SRC_OCR)
    else:
        warnings.append(W_FIELD_NOT_FOUND)

    return fields, warnings


def extract_iraqi_national_card(
    side_lines: list[SideLine],
) -> tuple[dict, list[str], mrz.MrzResult]:
    """Side-aware deterministic extraction for the Iraqi Unified National Card.

    Consumes side/ROI-tagged OCR lines (FRONT / BACK full-card Arabic OCR plus
    targeted ROI reads) and an MRZ pass, then builds the profile fields with
    source attribution and cross-checks. Never returns raw OCR text.
    """
    fields: dict[str, dict] = {}
    warnings: list[str] = []

    front: list[tuple[str, float]] = []
    back: list[tuple[str, float]] = []
    roi: dict[str, list[tuple[str, float]]] = {}
    back_texts: list[str] = []
    roi_mrz_texts: list[str] = []

    for line in side_lines:
        if not line.text:
            continue
        if line.side == "FRONT":
            front.append((line.text, line.confidence))
        elif line.side == "BACK":
            back.append((line.text, line.confidence))
            back_texts.append(line.text)
        else:
            roi.setdefault(line.side, []).append((line.text, line.confidence))
            if line.side == "ROI_MRZ":
                roi_mrz_texts.append(line.text)

    # ROI (Latin) MRZ lines first: they read line 3 cleanly; the Arabic back
    # lines remain as a fallback for line 1/2.
    mrz_input = list(roi_mrz_texts) + list(back_texts)

    fields["issuing_country"] = _candidate("IQ", 1.0, SRC_DOCUMENT_TYPE)

    # ---- MRZ (Iraqi card layout) ----
    mrz_result = mrz.parse_iraqi_national_card_mrz(mrz_input)
    if not mrz_result.detected:
        warnings.append(W_MRZ_NOT_DETECTED)
    elif not mrz_result.checks_passed or "MRZ_PARTIAL" in mrz_result.warnings:
        warnings.append(W_MRZ_PARTIAL)

    # ---- name / father / grandfather / mother (FRONT label-aware state machine)
    (
        name_value,
        name_conf,
        father_value,
        father_conf,
        grandfather_value,
        grandfather_conf,
        mother_value,
        mother_conf,
    ) = _parse_front_name_chain(front)
    if name_value:
        fields["name"] = _candidate(name_value, _clamp_conf(name_conf), SRC_FRONT)
    else:
        warnings.append(W_FIELD_MISSING)

    if father_value:
        fields["father_name"] = _candidate(
            father_value, _clamp_conf(father_conf), SRC_FRONT
        )
    else:
        warnings.append(W_FIELD_MISSING)

    if grandfather_value:
        fields["grandfather_name"] = _candidate(
            grandfather_value, _clamp_conf(grandfather_conf), SRC_FRONT
        )
    else:
        warnings.append(W_FIELD_MISSING)

    # Deterministic maternal evidence (M29.4). Present only when the card
    # front explicitly labels the mother's name; never guessed from OCR text.
    if mother_value:
        fields["mother_name"] = _candidate(
            mother_value, _clamp_conf(mother_conf), SRC_FRONT
        )

    # ---- sex (FRONT printed + MRZ cross-check) ----
    sex_value, sex_conf = _match_front_field(front, _SEX_LABELS)
    sex = _canonical_sex(sex_value) if sex_value else None
    mrz_sex = mrz_result.sex
    if sex and mrz_sex:
        agree = sex == ("MALE" if mrz_sex == "M" else "FEMALE")
        fields["sex"] = _candidate(
            sex,
            0.96 if agree else 0.45,
            SRC_FRONT,
            cross_check="MRZ_AGREE" if agree else "MRZ_MISMATCH",
        )
        if not agree:
            warnings.append(W_SOURCE_MISMATCH)
    elif sex:
        fields["sex"] = _candidate(sex, _clamp_conf(sex_conf, 0.9), SRC_FRONT)
    elif mrz_sex:
        fields["sex"] = _candidate(
            "MALE" if mrz_sex == "M" else "FEMALE", 0.9, SRC_MRZ
        )
    else:
        warnings.append(W_FIELD_MISSING)

    # ---- blood group (FRONT targeted ROI) ----
    blood = _extract_blood_group(roi.get("ROI_BLOOD", []))
    if blood:
        fields["blood_group"] = _candidate(blood, 0.82, SRC_ROI)
    else:
        warnings.append(W_BLOOD_GROUP_NOT_FOUND)

    # ---- national / card number (FRONT) + document_number alias ----
    card_value, card_conf, normalized = _extract_card_number(front)
    if card_value:
        conf = _clamp_conf(card_conf, 0.7)
        if normalized:
            conf = round(max(conf - 0.15, 0.3), 3)
            warnings.append(W_OCR_NORMALIZED)
        fields["national_card_number"] = _candidate(card_value, conf, SRC_FRONT)
        # Compatibility alias: IdentityDocument persists this in
        # document_number today. Schema unchanged this milestone.
        fields["document_number"] = _candidate(card_value, conf, SRC_FRONT)
    else:
        warnings.append(W_FIELD_MISSING)

    # ---- unique card body number (FRONT + MRZ cross-check) ----
    body_value = _extract_body_number(front)
    mrz_doc = mrz_result.document_number
    mrz_body = mrz_doc if _is_body_number(mrz_doc) else None
    if body_value or mrz_body:
        if body_value and mrz_body:
            if body_value == mrz_body:
                body_conf = 0.95
                cross = "MRZ_AGREE"
            else:
                body_conf = 0.7
                cross = "MRZ_MISMATCH"
                warnings.append(W_SOURCE_MISMATCH)
            source = SRC_FRONT
        elif body_value:
            body_conf = 0.9
            cross = None
            source = SRC_FRONT
        else:
            body_conf = 0.85
            cross = None
            source = SRC_MRZ
        fields["unique_card_body_number"] = _candidate(
            body_value or mrz_body, body_conf, source, cross_check=cross
        )
    else:
        warnings.append(W_FIELD_MISSING)

    # ---- date of birth (BACK printed + MRZ) ----
    dob_printed = _extract_dob(roi.get("ROI_DOB", []))
    mrz_dob = (
        mrz_result.date_of_birth.isoformat() if mrz_result.date_of_birth else None
    )
    dob_mrz_low = "date_of_birth" in mrz_result.low_confidence_fields
    if dob_printed and mrz_dob:
        if dob_printed == mrz_dob:
            fields["date_of_birth"] = _candidate(
                dob_printed, 0.94, SRC_BACK, cross_check="MRZ_AGREE"
            )
        else:
            fields["date_of_birth"] = _candidate(
                dob_printed, 0.45, SRC_BACK, cross_check="MRZ_MISMATCH"
            )
            warnings.append(W_SOURCE_MISMATCH)
    elif dob_printed:
        fields["date_of_birth"] = _candidate(dob_printed, 0.85, SRC_BACK)
    elif mrz_dob:
        fields["date_of_birth"] = _candidate(
            mrz_dob, 0.8 if not dob_mrz_low else 0.55, SRC_MRZ
        )
    else:
        warnings.append(W_FIELD_MISSING)

    # ---- issue / expiry (BACK printed; expiry cross-checked with MRZ) ----
    issue_value, expiry_printed = _extract_issue_expiry(roi.get("ROI_DATES", []))
    if issue_value:
        fields["issue_date"] = _candidate(issue_value, 0.7, SRC_BACK)
    expiry_mrz = (
        mrz_result.expiry_date.isoformat() if mrz_result.expiry_date else None
    )
    if expiry_mrz and expiry_printed:
        if expiry_mrz == expiry_printed:
            fields["expiry_date"] = _candidate(
                expiry_mrz, 0.94, SRC_MRZ, cross_check="MRZ_AGREE"
            )
        else:
            # MRZ is check-digit validated; prefer it over a truncated printed
            # read rather than emitting a plausible-looking wrong value.
            fields["expiry_date"] = _candidate(
                expiry_mrz, 0.65, SRC_MRZ, cross_check="MRZ_MISMATCH"
            )
            warnings.append(W_SOURCE_MISMATCH)
    elif expiry_mrz:
        fields["expiry_date"] = _candidate(expiry_mrz, 0.85, SRC_MRZ)
    elif expiry_printed:
        fields["expiry_date"] = _candidate(expiry_printed, 0.7, SRC_BACK)

    # ---- family number (BACK printed; MISSING safer than WRONG) ----
    family_value = _extract_family_number(
        roi.get("ROI_FAMILY", []), back, body_value
    )
    if family_value:
        fields["family_number"] = _candidate(family_value, 0.85, SRC_BACK)
    else:
        warnings.append(W_FAMILY_NOT_FOUND)

    return fields, warnings, mrz_result


def extract_identity(
    document_type: str, lines
) -> tuple[dict, list[str], dict]:
    """Run deterministic extraction. Returns (fields, warnings, mrz_summary).

    ``lines`` accepts either a list of plain strings (legacy: each line is
    treated as FRONT) or a list of SideLine objects (side/ROI tagged).
    """
    if not lines:
        warnings = [W_NO_TEXT] if _load_ocr() is not None else [W_OCR_UNAVAILABLE]
        return {}, warnings, {"detected": False, "valid": False, "checks_passed": False}

    if not isinstance(lines[0], SideLine):
        lines = [SideLine("FRONT", text) for text in lines]

    if document_type == "PASSPORT":
        text_lines = [line.text for line in lines]
        fields, warnings = extract_passport(text_lines)
        mrz_lines = _mrz_lines(text_lines)
        result = mrz.parse_mrz(mrz_lines) if mrz_lines else mrz.MrzResult()
        mrz_summary = {
            "detected": result.detected,
            "valid": result.valid,
            "checks_passed": result.checks_passed,
        }
    elif document_type == "UNIFIED_NATIONAL_CARD":
        fields, warnings, result = extract_iraqi_national_card(lines)
        mrz_summary = {
            "detected": result.detected,
            "valid": result.valid,
            "checks_passed": result.checks_passed,
        }
    else:
        return {}, [W_UNSUPPORTED_LAYOUT], {
            "detected": False,
            "valid": False,
            "checks_passed": False,
        }
    return fields, warnings, mrz_summary


def confidence_bucket(conf: float) -> str:
    """Coarse confidence bucket for SAFE logging only (no values)."""
    if conf >= 0.90:
        return "high"
    if conf >= 0.70:
        return "medium"
    return "low"
