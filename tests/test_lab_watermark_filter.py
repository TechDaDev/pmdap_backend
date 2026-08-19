"""Lab watermark / noise filtering tests (M20).

Synthetic spans only. Verifies the parser drops diagonal watermark strokes and
rejects non-unit content in the unit column (structurally, no hardcoded lab
names, no removal of legitimate Arabic), while keeping legitimate Arabic
results/references and OCR-degraded units intact. Canonical OCR spans are never
touched — only LabResult association excludes them.
"""
from datetime import date
from decimal import Decimal

import pytest
from django.conf import settings

from accounts.models import User
from documents.models import MedicalDocument, StoredFile
from labs.models import LabReportExtraction, LabResult
from labs.parsing import Span, parse_page
from labs.services import process_lab_extraction
from patients.models import PatientProfile
from processing.models import DocumentText, DocumentTextPage, DocumentTextSpan
from tests.test_lab_extraction import grid_spans

pytestmark = pytest.mark.django_db

LAB_PIPELINE_VERSION = settings.LAB_PIPELINE_VERSION

CHEM_HEADER = ["Item", "Result", "Unit", "Reference Range"]


def row_span(text, *, col, row=1, page=1, confidence=0.95, x_units=0.25, y_step=0.12):
    """One normal-height cell span (h = 0.08) at grid position (row, col)."""
    x0 = col * x_units
    x1 = x0 + (x_units - 0.05)
    y0 = row * y_step
    y1 = y0 + 0.08
    return Span(
        page_number=page,
        sequence=0,
        text=text,
        confidence=confidence,
        x_min=x0,
        y_min=y0,
        x_max=x1,
        y_max=y1,
    )


def watermark_span(text, *, x_min, x_max, row=1, height=0.16, page=1, confidence=0.9):
    """Oversized diagonal watermark stroke (h >> normal 0.08 cell height)."""
    y0 = row * 0.12
    y1 = y0 + height
    return Span(
        page_number=page,
        sequence=999,
        text=text,
        confidence=confidence,
        x_min=x_min,
        y_min=y0,
        x_max=x_max,
        y_max=y1,
    )


def chemistry_rows_with(extra_spans, *, rows=None, header=None):
    header = header or CHEM_HEADER
    rows = rows or [
        header,
        ["Creatinine", "1.0", "mg/dL", "0.7 - 1.18"],
    ]
    spans = grid_spans(rows, page=1)
    for span in extra_spans:
        spans.append(span)
    return spans


# --------------------------------------------------------------------------- #
# Geometric watermark filtering (parser, pure)
# --------------------------------------------------------------------------- #


def test_arabic_watermark_in_unit_column_is_dropped_real_unit_preserved():
    # Diagonal Arabic watermark overlapping the unit column region.
    spans = chemistry_rows_with(
        [watermark_span("/ومختبر البيلسان", x_min=0.42, x_max=0.67)]
    )
    results = parse_page(1, spans)

    assert len(results) == 1
    row = results[0]
    assert row.test_name_raw == "Creatinine"
    assert row.result_raw == "1.0"
    assert row.unit_raw == "mg/dL"  # real unit preserved
    assert row.reference_range_raw == "0.7 - 1.18"
    assert not any(ord(ch) >= 0x0621 for ch in row.unit_raw)
    assert row.evidence_sequence == (4, 5, 6, 7)  # watermark seq 999 excluded


def test_watermark_replacing_unit_column_leaves_unit_empty():
    # DOC_5 reproduction: watermark glyph covers the unit cell.
    rows = [
        CHEM_HEADER,
        ["S.creatinine*", "1.06", "/ومختبر البيلسان", "Normal : 0.7 - 1.18"],
    ]
    spans = grid_spans(rows)
    # Make the unit cell a diagonal oversized stroke (as OCR sees the watermark).
    spans = [
        s
        for s in spans
        if not (s.text == "/ومختبر البيلسان" and s.y_max - s.y_min < 0.10)
    ]
    spans.append(
        watermark_span("/ومختبر البيلسان", x_min=0.49, x_max=0.69, height=0.14)
    )
    results = parse_page(1, spans)

    assert len(results) == 1
    row = results[0]
    assert row.test_name_raw == "S.creatinine*"
    assert row.result_raw == "1.06"
    assert row.unit_raw == ""  # watermark never accepted as a unit
    assert "Normal" in row.reference_range_raw
    assert row.evidence_sequence == (4, 5, 7)  # watermark span excluded


def test_watermark_in_result_column_is_dropped():
    rows = [
        CHEM_HEADER,
        ["Creatinine", "/ومختبر البيلسان", "mg/dL", "0.7 - 1.18"],
    ]
    spans = chemistry_rows_with([], rows=rows)
    spans = [s for s in spans if s.text != "/ومختبر البيلسان"]
    spans.append(watermark_span("/ومختبر البيلسان", x_min=0.28, x_max=0.48))
    results = parse_page(1, spans)

    # No lab evidence after the stroke is dropped -> no row (never a fake result).
    assert results == []


def test_english_watermark_in_unit_column_is_dropped():
    spans = chemistry_rows_with(
        [watermark_span("AL-BAILASAN LABORATORY", x_min=0.42, x_max=0.67)]
    )
    results = parse_page(1, spans)

    assert len(results) == 1
    assert results[0].unit_raw == "mg/dL"
    assert "BAILASAN" not in results[0].unit_raw
    assert "BAILASAN" not in results[0].reference_range_raw


def test_repeated_watermark_phrase_across_rows_never_contaminates():
    rows = [
        CHEM_HEADER,
        ["Creatinine", "1.0", "mg/dL", "0.7 - 1.18"],
        ["Urea", "31.0", "mg/dL", "12.9 - 42.9"],
    ]
    spans = chemistry_rows_with([], rows=rows)
    spans.append(watermark_span("/ومختبر البيلسان", x_min=0.42, x_max=0.67, row=1))
    spans.append(watermark_span("/ومختبر البيلسان", x_min=0.42, x_max=0.67, row=2))
    results = parse_page(1, spans)

    assert len(results) == 2
    assert all(r.unit_raw == "mg/dL" for r in results)
    assert all(r.result_raw in {"1.0", "31.0"} for r in results)


# --------------------------------------------------------------------------- #
# Lexical unit validation (non-diagonal watermarks)
# --------------------------------------------------------------------------- #


def test_non_diagonal_arabic_in_unit_column_rejected_lexically():
    # Horizontal (normal-height) Arabic watermark in the unit column is not
    # caught by geometry, so the unit column lexical gate must reject it.
    rows = [
        CHEM_HEADER,
        ["Creatinine", "1.0", "/مختبر البيلسان", "0.7 - 1.18"],
    ]
    spans = grid_spans(rows)  # normal-height cells, watermark not oversized
    results = parse_page(1, spans)

    assert len(results) == 1
    assert results[0].unit_raw == ""  # lexical validation drops it
    assert results[0].result_raw == "1.0"
    assert results[0].reference_range_raw == "0.7 - 1.18"


def test_garbled_cell_count_unit_is_preserved():
    # OCR-degraded x10^3/µL -> "x10%μL" must survive (no v1 regression).
    rows = [
        ["Test", "Result", "Units", "Low", "High"],
        ["WBC", "6.71", "x10%μL", "3.60", "10.20"],
    ]
    results = parse_page(1, grid_spans(rows))
    assert len(results) == 1
    assert results[0].test_name_raw == "WBC"
    assert results[0].unit_raw == "x10%μL"
    assert results[0].result_raw == "6.71"


def test_legitimate_arabic_result_and_reference_stay():
    # Legitimate Arabic content inside the correct columns must never be
    # filtered out.
    rows = [
        CHEM_HEADER,
        ["HB Antigen", "إيجابي", "", "نطاق طبيعي 0.3 - 1.2"],
    ]
    results = parse_page(1, grid_spans(rows))

    assert len(results) == 1
    assert results[0].test_name_raw == "HB Antigen"
    assert results[0].result_raw == "إيجابي"
    assert "0.3 - 1.2" in results[0].reference_range_raw


def test_legitimate_long_arabic_reference_in_reference_column_stays():
    rows = [
        CHEM_HEADER,
        ["Vitamin D3", "19.3", "ng/mL", "الكفاية 29 - 100"],
    ]
    results = parse_page(1, grid_spans(rows))

    assert len(results) == 1
    assert results[0].unit_raw == "ng/mL"
    assert results[0].result_raw == "19.3"
    assert "29 - 100" in results[0].reference_range_raw


# --------------------------------------------------------------------------- #
# DB roundtrip: source_spans exclude watermark, canonical OCR untouched
# --------------------------------------------------------------------------- #


def _make_lab_document(rows):
    user = User.objects.create_user(
        email="watermark@example.com",
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    profile = PatientProfile.objects.create(
        user=user,
        digital_id="2" * 17,
        full_name="Synthetic",
        date_of_birth=date(1990, 1, 2),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )
    stored = StoredFile.objects.create(
        file="medical/wm.jpg",
        original_filename="wm.jpg",
        mime_type="image/jpeg",
        size_bytes=123,
        sha256="c" * 64,
        page_count=1,
        integrity_status=StoredFile.IntegrityStatus.VALID,
        malware_scan_status=StoredFile.MalwareScanStatus.CLEAN,
    )
    document = MedicalDocument.objects.create(
        patient=profile,
        uploaded_by=user,
        stored_file=stored,
        content_sha256="d" * 64,
        document_type="LABORATORY",
        processing_status="TEXT_EXTRACTED",
    )
    text = DocumentText.objects.create(
        document=document,
        text="Synthetic",
        page_count=1,
        character_count=9,
        meaningful_character_count=9,
        usable=True,
        usability_reason="ok",
        has_pages_requiring_ocr=False,
        extraction_method=DocumentText.ExtractionMethod.OCR,
        extractor_name="paddleocr",
        extractor_version="3.7.0",
        pipeline_version="m8-ocr-v1",
    )
    page = DocumentTextPage.objects.create(
        document_text=text,
        page_number=1,
        text="Synthetic",
        ocr_text="Synthetic",
        meaningful_character_count=9,
        effective_source="OCR",
    )
    seq = 0
    for span in grid_spans(rows):
        DocumentTextSpan.objects.create(
            document_text_page=page,
            sequence=seq,
            text=span.text,
            confidence=span.confidence,
            x_min=span.x_min,
            y_min=span.y_min,
            x_max=span.x_max,
            y_max=span.y_max,
            source="OCR",
            page_width=320,
            page_height=120,
        )
        seq += 1
    # inject the diagonal watermark as a real OCR span (canonical, must stay)
    wm = watermark_span("/ومختبر البيلسان", x_min=0.42, x_max=0.67)
    DocumentTextSpan.objects.create(
        document_text_page=page,
        sequence=seq,
        text=wm.text,
        confidence=wm.confidence,
        x_min=wm.x_min,
        y_min=wm.y_min,
        x_max=wm.x_max,
        y_max=wm.y_max,
        source="OCR",
        page_width=320,
        page_height=120,
    )
    return document, page


def test_db_roundtrip_watermark_excluded_from_lab_result_source_spans():
    document, page = _make_lab_document(
        [
            CHEM_HEADER,
            ["Creatinine", "1.0", "mg/dL", "0.7 - 1.18"],
        ]
    )
    result_code = process_lab_extraction(str(document.uuid))
    assert result_code in {"COMPLETED", "lab-v2"}
    extraction = (
        LabReportExtraction.objects.filter(document=document)
        .order_by("-created_at")
        .first()
    )
    assert extraction.pipeline_version == LAB_PIPELINE_VERSION
    rows = list(LabResult.objects.filter(extraction=extraction))
    assert len(rows) == 1
    row = rows[0]
    assert row.unit_raw == "mg/dL"
    assert "/ومختبر البيلسان" not in row.unit_raw
    assert row.result_raw == "1.0"
    # watermark span still canonical in DocumentTextSpan
    canonical = DocumentTextSpan.objects.filter(
        document_text_page=page, text__contains="البيلسان"
    )
    assert canonical.exists()
    # ... but not linked to the lab result
    assert not row.source_spans.filter(text__contains="البيلسان").exists()


def test_db_roundtrip_v2_replaces_v1_and_stays_idempotent():
    document, _ = _make_lab_document(
        [
            CHEM_HEADER,
            ["Creatinine", "1.0", "mg/dL", "0.7 - 1.18"],
        ]
    )
    first = process_lab_extraction(str(document.uuid))
    second = process_lab_extraction(str(document.uuid))
    assert first == second
    extraction = (
        LabReportExtraction.objects.filter(document=document)
        .order_by("-created_at")
        .first()
    )
    assert extraction.pipeline_version == LAB_PIPELINE_VERSION
    assert extraction.result_count == 1
    assert LabResult.objects.filter(extraction=extraction).count() == 1
