"""
M16.1 regression tests for the M9 remediation.

Covers cross-line label association, Date-of-Application distinction, the
"issued" label, Arabic date labels (western / Arabic-Indic / Persian digits),
compact labeled date recovery, and identifier-safe guards. All synthetic;
no real patient content.
"""

from datetime import date

import pytest

from processing.dates import (
    CandidateType,
    choose_suggested_index,
    detect_page_dates,
)

pytestmark = pytest.mark.django_db

TODAY = date(2026, 8, 10)
ARABIC_INDIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
PERSIAN = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def detect(text):
    return detect_page_dates(text, page_number=1, source="OCR", today=TODAY)


def suggested(text):
    candidates = detect(text)
    index = choose_suggested_index(candidates)
    return candidates[index] if index is not None else None


def test_report_dob_application_same_page():
    text = (
        "Report Date: 17/09/2025\nBirthday: 01/01/1986\nDate of Application: 14/09/2025"
    )
    candidates = detect(text)
    by_date = {c.detected_date: c for c in candidates}
    assert by_date[date(2025, 9, 17)].candidate_type == CandidateType.REPORT_DATE
    assert by_date[date(1986, 1, 1)].candidate_type == CandidateType.DATE_OF_BIRTH
    assert by_date[date(2025, 9, 14)].candidate_type == CandidateType.APPLICATION_DATE
    assert suggested(text).detected_date == date(2025, 9, 17)


def test_application_date_never_wins_over_report_date():
    text = "Date of Application : 14/9/2025 09:59\nReport Date : 17/09/2025 11:28"
    candidate = suggested(text)
    assert candidate is not None
    assert candidate.detected_date == date(2025, 9, 17)
    assert candidate.candidate_type == CandidateType.REPORT_DATE


def test_adjacent_line_report_date_english():
    text = "Report\nDate\n: 17/09/2025 11:28:34"
    candidate = suggested(text)
    assert candidate is not None
    assert candidate.detected_date == date(2025, 9, 17)
    assert candidate.candidate_type == CandidateType.REPORT_DATE


def test_adjacent_line_arabic_label_western_digits():
    text = "تقرير المختبر\nتاريخ التقرير\n14/03/2026"
    candidate = suggested(text)
    assert candidate is not None
    assert candidate.detected_date == date(2026, 3, 14)
    assert candidate.candidate_type == CandidateType.REPORT_DATE


def test_adjacent_line_dob_not_misassociated_with_report_label():
    text = "Birthday\n01/01/1986\n\nReport Date\n17/09/2025"
    candidates = detect(text)
    by_date = {c.detected_date: c for c in candidates}
    assert by_date[date(1986, 1, 1)].candidate_type == CandidateType.DATE_OF_BIRTH
    assert by_date[date(2025, 9, 17)].candidate_type == CandidateType.REPORT_DATE
    assert suggested(text).detected_date == date(2025, 9, 17)


def test_arabic_indic_digits_same_line():
    text = "تاريخ التقرير: ١٤/٠٣/٢٠٢٦"
    candidate = suggested(text)
    assert candidate is not None
    assert candidate.detected_date == date(2026, 3, 14)


def test_persian_digits_same_line():
    text = "تاريخ التقرير: ۱۴/۰۳/۲۰۲۶"
    candidate = suggested(text)
    assert candidate is not None
    assert candidate.detected_date == date(2026, 3, 14)


def test_multiple_adjacent_labels_bounded():
    # A date with two label lines adjacent must pick the explicit one.
    text = "Report Date\n17/09/2025\nPrinted\n16/09/2025"
    candidate = suggested(text)
    assert candidate is not None
    assert candidate.detected_date == date(2025, 9, 17)


def test_compact_labeled_report_date():
    text = "Report Date\n17092025"
    candidate = suggested(text)
    assert candidate is not None
    assert candidate.detected_date == date(2025, 9, 17)
    assert candidate.parsing_rule == "COMPACT_DMY"


def test_compact_dob_blocked():
    text = "Birthday\n01011986"
    assert suggested(text) is None


def test_identifier_like_eight_digits_not_parsed():
    text = "Patient Number : 17936631\nReport Date\n17/09/2025"
    candidate = suggested(text)
    assert candidate is not None
    assert candidate.detected_date == date(2025, 9, 17)
    # The identifier-like 8-digit run must not have created a date candidate.
    assert all(
        c.parsing_rule != "COMPACT_DMY" or c.detected_date == date(2025, 9, 17)
        for c in detect(text)
    )


def test_compact_future_date_blocked():
    text = "Report Date\n17102040"
    assert suggested(text) is None


def test_issued_label_now_suggested():
    text = "PATHOLOGY DEPARTMENT\nIssued 14/03/2026\nPage 1"
    candidate = suggested(text)
    assert candidate is not None
    assert candidate.detected_date == date(2026, 3, 14)
    assert candidate.candidate_type == CandidateType.ISSUE_DATE


# ---------------------------------------------------------------------------
# Arabic-Indic medical report dates (separators dropped by OCR).
# A printed `التاريخ: ٢٠٢١/٠١/٠٢` can OCR as `التاريخ٢٠٢١٠١٠٢` (glued, no
# slashes). The 8-digit run must be read as YYYYMMDD. All synthetic.
# ---------------------------------------------------------------------------


def _detect_one(text):
    candidates = detect(text)
    assert len(candidates) == 1, [c.parsing_rule for c in candidates]
    return candidates[0]


def test_arabic_indic_glued_compact_ymd():
    text = "التاريخ٢٠٢١٠١٠٢"
    candidate = _detect_one(text)
    assert candidate.detected_date == date(2021, 1, 2)
    assert candidate.parsing_rule == "COMPACT_YMD"
    assert candidate.candidate_type == CandidateType.REPORT_DATE


def test_persian_glued_compact_ymd():
    text = "التاريخ۲۰۲۱۰۱۰۲"
    candidate = _detect_one(text)
    assert candidate.detected_date == date(2021, 1, 2)
    assert candidate.parsing_rule == "COMPACT_YMD"
    assert candidate.candidate_type == CandidateType.REPORT_DATE


def test_ascii_compact_ymd_under_label():
    text = "Report Date\n20210102"
    candidate = _detect_one(text)
    assert candidate.detected_date == date(2021, 1, 2)
    assert candidate.parsing_rule == "COMPACT_YMD"


def test_compact_dmy_still_supported():
    # A DMY 8-digit run must keep resolving (English reports).
    text = "Report Date\n02012026"
    candidate = _detect_one(text)
    assert candidate.detected_date == date(2026, 1, 2)
    assert candidate.parsing_rule == "COMPACT_DMY"


def test_arabic_indic_with_spaced_separators():
    text = "التاريخ : ٢٠٢١ / ٠١ / ٠٢ م"
    candidate = _detect_one(text)
    assert candidate.detected_date == date(2021, 1, 2)
    assert candidate.parsing_rule == "YMD_NUMERIC"
    assert candidate.candidate_type == CandidateType.REPORT_DATE


def test_rtl_label_after_date():
    text = "٢٠٢١/٠١/٠٢ :التاريخ"
    candidate = _detect_one(text)
    assert candidate.detected_date == date(2021, 1, 2)
    assert candidate.candidate_type == CandidateType.REPORT_DATE


def test_mixed_digit_scripts_normalize():
    text = "تاريخ التقرير: ٢٠2١/٠١/02"
    candidate = _detect_one(text)
    assert candidate.detected_date == date(2021, 1, 2)


def test_glued_label_with_digits_matches_label():
    # The label is directly glued to the digit value; it must still classify
    # as REPORT_DATE (not drift to UNKNOWN).
    candidate = _detect_one("التاريخ٢٠٢١٠١٠٢")
    assert candidate.candidate_type == CandidateType.REPORT_DATE


def test_arabic_phone_number_not_a_date():
    assert detect("الهاتف: ٠٧٧٠١٢٣٤٥٦٧") == ()


def test_arabic_patient_id_not_a_date():
    assert detect("رقم المريض: ٣٠٣٠٠٠") == ()


def test_arabic_age_not_a_date():
    assert detect("٤٠ سنة") == ()


def test_identifier_like_eight_digits_under_number_hint_blocked():
    # Even a calendar-valid 8-digit run is rejected under an identifier hint.
    text = "رقم المريض: ١٧٩٣٦٦٣١\nReport Date\n17/09/2025"
    candidate = suggested(text)
    assert candidate is not None
    assert candidate.detected_date == date(2025, 9, 17)
    assert all(
        c.parsing_rule not in {"COMPACT_YMD", "COMPACT_DMY"}
        or c.detected_date == date(2025, 9, 17)
        for c in detect(text)
    )


def test_arabic_word_continuation_not_mislabeled():
    # "تاريخي" must not match the label "تاريخ"; the date stays UNKNOWN.
    candidates = detect("تاريخي ٢٠٢١/٠١/٠٢")
    assert len(candidates) == 1
    assert candidates[0].candidate_type != CandidateType.REPORT_DATE
