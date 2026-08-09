"""
M16 regression fixtures for the M9 deterministic date pipeline.

These tests exercise the production M9 parser on OCR-output-like text that the
M16 benchmark captured, WITHOUT re-running the OCR engine in CI. They lock in
the observed behavior so future changes to M9 or the benchmark are visible.

Deliberately not tied to the PaddleOCR engine (deterministic + fast).
"""

from datetime import date

import pytest

from processing.dates import (
    CandidateType,
    choose_suggested_index,
    detect_page_dates,
)

pytestmark = pytest.mark.django_db

TODAY = date(2026, 8, 9)


def detect(text):
    return detect_page_dates(text, page_number=1, source="OCR", today=TODAY)


def suggested(text):
    candidates = detect(text)
    index = choose_suggested_index(candidates)
    return candidates[index] if index is not None else None


def test_english_simple_report_date():
    text = "MEDICAL LABORATORY REPORT\nReport Date: 14/03/2026\nHemoglobin: 13.4"
    candidate = suggested(text)
    assert candidate is not None
    assert candidate.detected_date == date(2026, 3, 14)
    assert candidate.candidate_type == CandidateType.REPORT_DATE


def test_multiple_dates_dob_deprioritized():
    text = (
        "Date of Birth: 21/06/1985\n"
        "Collection Date: 12/07/2026\n"
        "Report Date: 14/07/2026\n"
        "Print Date: 15/07/2026"
    )
    candidate = suggested(text)
    assert candidate is not None
    assert candidate.detected_date == date(2026, 7, 14)
    assert candidate.candidate_type == CandidateType.REPORT_DATE


def test_dob_never_wins_against_report_date():
    text = "Date of Birth: 21/06/1985\nReport Date: 14/03/2026"
    candidate = suggested(text)
    assert candidate is not None
    assert candidate.detected_date == date(2026, 3, 14)
    assert candidate.candidate_type == CandidateType.REPORT_DATE


def test_print_date_loses_to_report_date():
    text = "Report Date: 14/03/2026\nPrint Date: 16/03/2026"
    candidate = suggested(text)
    assert candidate is not None
    assert candidate.detected_date == date(2026, 3, 14)


def test_arabic_separate_line_date_suggested_via_cross_line():
    # M16.1 remediation: a date on its own line below the Arabic label is now
    # associated via the adjacent line and suggested as REPORT_DATE.
    text = "تقرير المختبر الطبي\nتاريخ التقرير\n14/03/2026\nالهيموغلوبين: 13.4"
    candidates = detect(text)
    assert any(c.detected_date == date(2026, 3, 14) for c in candidates)
    candidate = suggested(text)
    assert candidate is not None
    assert candidate.detected_date == date(2026, 3, 14)
    assert candidate.candidate_type == CandidateType.REPORT_DATE


def test_inline_arabic_western_date_digits_absent():
    # Mirrors what the benchmark observed: inline western digits in Arabic
    # lines are dropped by the OCR recognizer, so no candidate exists.
    text = "مختبر التحاليل الطبية\nتاريخ التقرير\nالهيموغلوبين: 13.4"
    candidates = detect(text)
    assert not any(c.detected_date == date(2026, 3, 14) for c in candidates)


def test_footer_issued_label_classified_issue_date():
    # M16.1 remediation: bare "Issued <date>" is now an explicit ISSUE_DATE
    # label and is suggested (previously UNKNOWN / no suggestion).
    text = "PATHOLOGY DEPARTMENT\nResult: Normal\nIssued 14/03/2026\nPage 1 of 1"
    candidates = detect(text)
    assert any(c.detected_date == date(2026, 3, 14) for c in candidates)
    candidate = suggested(text)
    assert candidate is not None
    assert candidate.detected_date == date(2026, 3, 14)
    assert candidate.candidate_type == CandidateType.ISSUE_DATE


def test_bare_ambiguous_date_withheld():
    text = "03/04/2026\nSample result text here"
    assert suggested(text) is None


def test_named_month_english():
    text = "REPORT\nReport Date: 14 March 2026"
    candidate = suggested(text)
    assert candidate is not None
    assert candidate.detected_date == date(2026, 3, 14)
