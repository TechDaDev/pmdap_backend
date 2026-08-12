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

from identities import mrz

logger = logging.getLogger(__name__)

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
_NATIONAL_RE = re.compile(r"\b\d{15}\b")
_DOC_NUMBER_RE = re.compile(r"\b[A-Z0-9]{6,12}\b")
_FAMILY_RE = re.compile(r"\b\d{3,5}\b")
_ISSUE_LABELS = ("issue date", "date of issue", "issued on", "date issued")
_MRZ_LINES = re.compile(r"^[A-Z0-9<]{30,44}$")


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


def _candidate(value: str | None, confidence: float, source: str) -> dict:
    return {
        "value": value,
        "confidence": round(min(max(confidence, 0.0), 1.0), 3),
        "source": source,
    }


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


def extract_national_card(lines: list[str]) -> tuple[dict, list[str]]:
    fields: dict[str, dict] = {}
    warnings: list[str] = []

    fields["issuing_country"] = _candidate("IQ", 1.0, SRC_DOCUMENT_TYPE)

    joined = "\n".join(lines).upper()
    nat = _NATIONAL_RE.search(joined)
    if nat:
        fields["national_number"] = _candidate(nat.group(0), 0.7, SRC_OCR)
    else:
        warnings.append(W_FIELD_NOT_FOUND)

    # Family number: short numeric run not part of the national number.
    family = _FAMILY_RE.search(joined)
    if family:
        fields["family_number"] = _candidate(family.group(0), 0.55, SRC_OCR)
    else:
        warnings.append(W_FIELD_NOT_FOUND)

    doc = _DOC_NUMBER_RE.search(joined)
    if doc and doc.group(0) not in {nat.group(0) if nat else ""}:
        fields["document_number"] = _candidate(doc.group(0), 0.5, SRC_OCR)
    else:
        warnings.append(W_FIELD_NOT_FOUND)

    return fields, warnings


def extract_identity(document_type: str, lines: list[str]) -> tuple[dict, list[str], dict]:
    """Run deterministic extraction. Returns (fields, warnings, mrz_summary)."""
    if not lines:
        warnings = [W_NO_TEXT] if _load_ocr() is not None else [W_OCR_UNAVAILABLE]
        return {}, warnings, {"detected": False, "valid": False, "checks_passed": False}

    if document_type == "PASSPORT":
        fields, warnings = extract_passport(lines)
    elif document_type == "UNIFIED_NATIONAL_CARD":
        fields, warnings = extract_national_card(lines)
    else:
        return {}, [W_UNSUPPORTED_LAYOUT], {
            "detected": False,
            "valid": False,
            "checks_passed": False,
        }

    mrz_lines = _mrz_lines(lines)
    result = mrz.parse_mrz(mrz_lines) if mrz_lines else mrz.MrzResult()
    mrz_summary = {
        "detected": result.detected,
        "valid": result.valid,
        "checks_passed": result.checks_passed,
    }
    return fields, warnings, mrz_summary
