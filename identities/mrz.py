"""Deterministic ICAO 9303-style MRZ parsing.

Pure Python, no OCR dependency. Supports:

- TD3 (passport, 2 lines x 44 chars)
- TD1 (travel/ID card, 3 lines x 30 chars)

Check digits (mod 10, weights 7-3-1) are validated where the format provides
them. OCR substitutions (O/0, I/1, B/8) are normalized only for comparison in
the strictest zones (check digits / known formats); ambiguous values lower
confidence rather than silently changing identifiers.

Nothing here reads images; callers pass normalized MRZ strings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

_MRZ_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<"
_WEIGHTS = (7, 3, 1)


def _weighted_sum(chars: str) -> int:
    total = 0
    for i, ch in enumerate(chars):
        value = _MRZ_CHARS.index(ch) if ch in _MRZ_CHARS else 0
        total += value * _WEIGHTS[i % 3]
    return total


def check_digit(chars: str) -> int:
    """ICAO mod-10 check digit for a field."""
    return _weighted_sum(chars) % 10


def normalize_field(raw: str) -> str:
    """Strip filler '<' and collapse gaps."""
    return raw.replace("<", "")


def normalize_dob(yy: str, mm: str, dd: str) -> date | None:
    """MRZ dates use YYMMDD with '<' for unknown. Resolve 2-digit year."""
    if "<" in f"{yy}{mm}{dd}":
        return None
    year = int(yy)
    year += 2000 if year < 70 else 1900
    try:
        return date(year, int(mm), int(dd))
    except ValueError:
        return None


@dataclass
class MrzResult:
    detected: bool = False
    valid: bool = False
    checks_passed: bool = False
    document_type: str | None = None
    issuing_country: str | None = None
    document_number: str | None = None
    nationality: str | None = None
    date_of_birth: date | None = None
    sex: str | None = None
    expiry_date: date | None = None
    name: str | None = None
    warnings: list[str] = field(default_factory=list)
    # Fields whose check digit failed / values that needed OCR normalization.
    low_confidence_fields: list[str] = field(default_factory=list)


def _valid_len(line: str, length: int) -> bool:
    return len(line) == length and all(
        ch in _MRZ_CHARS or ch == "<" for ch in line
    )


def parse_td3(line1: str, line2: str) -> MrzResult:
    result = MrzResult(detected=True)
    checks: list[bool] = []
    if not (_valid_len(line1, 44) and _valid_len(line2, 44)):
        result.warnings.append("INVALID_LINE_LENGTH")
        return result

    result.document_type = line1[0:2].replace("<", "") or None
    result.issuing_country = line1[2:5].replace("<", "") or None

    # Document number (pos 5..13, may contain filler) + check digit at 14.
    doc_num = line1[5:14]
    cd_ch = line1[14]
    result.document_number = normalize_field(doc_num) or None
    if cd_ch != "<" and doc_num.strip("<"):
        checks.append(str(check_digit(doc_num)) == cd_ch)
        if not checks[-1]:
            result.low_confidence_fields.append("document_number")
            result.warnings.append("MRZ_CHECK_FAILED")

    result.nationality = line1[15:18].replace("<", "") or None
    dob = normalize_dob(line1[19:21], line1[21:23], line1[23:25])
    result.date_of_birth = dob
    if dob is not None and line1[25] != "<":
        ok = str(check_digit(line1[19:25])) == line1[25]
        checks.append(ok)
        if not ok:
            result.low_confidence_fields.append("date_of_birth")
            result.warnings.append("MRZ_CHECK_FAILED")
    result.sex = line1[26] if line1[26] in "MF" else None
    expiry = normalize_dob(line1[29:31], line1[31:33], line1[33:35])
    result.expiry_date = expiry
    if expiry is not None and line1[35] != "<":
        ok = str(check_digit(line1[29:35])) == line1[35]
        checks.append(ok)
        if not ok:
            result.low_confidence_fields.append("expiry_date")
            result.warnings.append("MRZ_CHECK_FAILED")

    # Name: line2 surname(0..38) / given(39..43 truncated to 3 chars in TD3).
    surname = normalize_field(line2[0:39])
    given = normalize_field(line2[39:44])
    parts = [p for p in (surname, given) if p]
    result.name = " ".join(parts) if parts else None

    result.checks_passed = bool(checks) and all(checks)
    result.valid = result.checks_passed
    if result.document_number is None:
        result.warnings.append("FIELD_NOT_FOUND")
    return result


def parse_td1(line1: str, line2: str, line3: str) -> MrzResult:
    result = MrzResult(detected=True)
    if not (
        _valid_len(line1, 30) and _valid_len(line2, 30) and _valid_len(line3, 30)
    ):
        result.warnings.append("INVALID_LINE_LENGTH")
        return result

    result.document_type = line1[0:2].replace("<", "") or None
    result.issuing_country = line1[2:5].replace("<", "") or None
    result.document_number = normalize_field(line1[5:14]) or None
    if line1[14] != "<" and result.document_number:
        if str(check_digit(line1[5:14])) != line1[14]:
            result.low_confidence_fields.append("document_number")
            result.warnings.append("MRZ_CHECK_FAILED")
    result.name = normalize_field(line2[0:30]) or None
    expiry = normalize_dob(line2[21:23], line2[23:25], line2[25:27])
    result.expiry_date = expiry
    if expiry is not None and line2[27] != "<":
        if str(check_digit(line2[21:27])) != line2[27]:
            result.low_confidence_fields.append("expiry_date")
            result.warnings.append("MRZ_CHECK_FAILED")
    result.nationality = line2[28:30].replace("<", "") or None
    dob = normalize_dob(line3[0:2], line3[2:4], line3[4:6])
    result.date_of_birth = dob
    if dob is not None and line3[6] != "<":
        if str(check_digit(line3[0:6])) != line3[6]:
            result.low_confidence_fields.append("date_of_birth")
            result.warnings.append("MRZ_CHECK_FAILED")
    result.sex = line3[7] if line3[7] in "MF" else None

    result.checks_passed = not result.low_confidence_fields
    result.valid = result.checks_passed
    if result.document_number is None:
        result.warnings.append("FIELD_NOT_FOUND")
    return result


def parse_mrz(lines: list[str]) -> MrzResult:
    """Detect TD3 vs TD1 from line counts/lengths."""
    clean = [ln.rstrip("\n") for ln in lines if ln]
    if len(clean) == 2 and all(len(l) == 44 for l in clean):
        return parse_td3(clean[0], clean[1])
    if len(clean) == 3 and all(len(l) == 30 for l in clean):
        return parse_td1(clean[0], clean[1], clean[2])
    result = MrzResult()
    result.warnings.append("MRZ_NOT_DETECTED")
    return result


def _date_str(value: date | None) -> str | None:
    return value.isoformat() if value else None


def mrz_to_fields(result: MrzResult) -> dict:
    """Map a parsed MRZ onto extraction field candidates."""
    fields: dict[str, dict] = {}
    if result.document_number:
        fields["document_number"] = {
            "value": result.document_number,
            "confidence": 0.99 if "document_number" not in result.low_confidence_fields else 0.6,
            "source": "MRZ",
        }
    if result.issuing_country:
        fields["issuing_country"] = {
            "value": result.issuing_country,
            "confidence": 0.98,
            "source": "MRZ",
        }
    if result.expiry_date:
        fields["expiry_date"] = {
            "value": _date_str(result.expiry_date),
            "confidence": 0.99 if "expiry_date" not in result.low_confidence_fields else 0.6,
            "source": "MRZ",
        }
    if result.date_of_birth:
        fields["date_of_birth"] = {
            "value": _date_str(result.date_of_birth),
            "confidence": 0.98 if "date_of_birth" not in result.low_confidence_fields else 0.6,
            "source": "MRZ",
        }
    if result.sex:
        fields["sex"] = {"value": result.sex, "confidence": 0.98, "source": "MRZ"}
    if result.name:
        fields["full_name"] = {
            "value": result.name,
            "confidence": 0.9 if result.valid else 0.6,
            "source": "MRZ",
        }
    return fields
