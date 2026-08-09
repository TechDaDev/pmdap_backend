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
