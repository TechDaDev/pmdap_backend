from datetime import date

import pytest

from processing.dates import (
    CandidateType,
    choose_suggested_index,
    detect_page_dates,
    normalize_text,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("١٤/٠٣/٢٠٢٦", "14/03/2026"),
        ("۱۴/۰۳/۲۰۲۶", "14/03/2026"),
        ("١٤/03/٢٠٢٦", "14/03/2026"),
        ("Report\t Date:   14⁄03⁄2026", "Report Date: 14/03/2026"),
    ],
)
def test_normalization_is_deterministic_and_preserves_raw_mapping(raw, expected):
    normalized = normalize_text(raw)

    assert normalized.text == expected
    assert normalized.raw_slice(0, len(normalized.text)) == raw


@pytest.mark.parametrize(
    "value",
    [
        "14/03/2026",
        "14-03-2026",
        "14.03.2026",
        "2026-03-14",
        "14/3/2026",
        "14 Mar 2026",
        "14 March 2026",
        "March 14 2026",
        "March 14, 2026",
    ],
)
def test_required_date_formats_parse_to_same_date(value):
    candidate = detect_page_dates(
        f"Report Date: {value}", page_number=1, source="PDF_TEXT"
    )[0]

    assert candidate.detected_date == date(2026, 3, 14)
    assert candidate.candidate_type == CandidateType.REPORT_DATE


def test_leap_year_and_impossible_dates_are_validated():
    assert detect_page_dates(
        "Report Date: 29/02/2024", page_number=1, source="PDF_TEXT"
    )
    for value in (
        "29/02/2025",
        "31/02/2026",
        "32/01/2026",
        "00/12/2026",
        "2026-13-01",
    ):
        assert not detect_page_dates(
            f"Report Date: {value}", page_number=1, source="PDF_TEXT"
        )


def test_ambiguous_numeric_date_preserves_dmy_and_mdy_interpretations():
    candidate = detect_page_dates("03/04/2026", page_number=1, source="PDF_TEXT")[0]

    assert candidate.detected_date == date(2026, 4, 3)
    assert candidate.alternative_date == date(2026, 3, 4)
    assert candidate.ambiguous is True
    assert candidate.parsing_rule == "DMY_NUMERIC"
    assert candidate.candidate_type == CandidateType.UNKNOWN
    assert choose_suggested_index((candidate,)) is None


def test_arabic_labels_and_digits_classify_semantically():
    candidates = detect_page_dates(
        "تاريخ التقرير: ١٤/٠٣/٢٠٢٦\nتاريخ الميلاد: ٢١/٠٦/١٩٨٥\nتاريخ الفحص: ١٢/٠٧/٢٠٢٦",
        page_number=2,
        source="OCR",
    )

    assert [candidate.candidate_type for candidate in candidates] == [
        CandidateType.REPORT_DATE,
        CandidateType.DATE_OF_BIRTH,
        CandidateType.EXAMINATION_DATE,
    ]
    assert all(candidate.source == "OCR" for candidate in candidates)
    assert candidates[0].raw_value == "١٤/٠٣/٢٠٢٦"


def test_multiple_dates_rank_report_date_and_dob_never_wins():
    candidates = detect_page_dates(
        "DOB: 21/06/1985\n"
        "Collection Date: 12/07/2026\n"
        "Report Date: 13/07/2026\n"
        "Printed: 14/07/2026",
        page_number=1,
        source="PDF_TEXT",
        today=date(2026, 7, 20),
    )

    assert len(candidates) == 4
    suggested = choose_suggested_index(candidates)
    assert suggested is not None
    assert candidates[suggested].detected_date == date(2026, 7, 13)
    assert candidates[suggested].candidate_type == CandidateType.REPORT_DATE
    scores = {candidate.candidate_type: candidate.score for candidate in candidates}
    assert scores[CandidateType.REPORT_DATE] > scores[CandidateType.COLLECTION_DATE]
    assert scores[CandidateType.PRINT_DATE] > scores[CandidateType.DATE_OF_BIRTH]


def test_mixed_language_report_suggests_arabic_report_date_not_iso_dob():
    candidates = detect_page_dates(
        "Patient Name: Synthetic Patient\n"
        "DOB: 1980-10-20\n"
        "تاريخ التقرير: ١٣/٧/٢٠٢٦\n"
        "Hospital: Synthetic Facility",
        page_number=1,
        source="OCR",
        today=date(2026, 7, 20),
    )

    suggested = choose_suggested_index(candidates)
    assert suggested is not None
    assert candidates[suggested].detected_date == date(2026, 7, 13)
    assert candidates[suggested].candidate_type == CandidateType.REPORT_DATE


def test_future_date_is_strongly_penalized_but_historical_date_is_not():
    candidates = detect_page_dates(
        "Report Date: 10/09/2026\nIssue Date: 10/09/2001",
        page_number=1,
        source="PDF_TEXT",
        today=date(2026, 7, 1),
        future_tolerance_days=14,
    )

    assert candidates[0].score < candidates[1].score
    assert candidates[1].score >= 0.75


def test_context_is_bounded_and_control_characters_are_removed():
    candidates = detect_page_dates(
        "X" * 200 + "\x00\x1bReport Date: 14/03/2026" + "Y" * 200,
        page_number=1,
        source="PDF_TEXT",
        context_max_chars=80,
    )

    assert len(candidates[0].context) <= 80
    assert "\x00" not in candidates[0].context
    assert "\x1b" not in candidates[0].context


def test_different_top_dates_tied_within_tolerance_have_no_suggestion():
    candidates = detect_page_dates(
        "Report Date: 13/07/2026\nReport Date: 14/07/2026",
        page_number=1,
        source="PDF_TEXT",
        today=date(2026, 7, 20),
    )

    assert choose_suggested_index(candidates) is None
