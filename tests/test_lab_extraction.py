"""Structured lab extraction tests.

Synthetic OCR spans only (invented values). Covers geometry parsing, DB
roundtrip, idempotency, non-fatal failure, privacy guards, document-type gate,
and the pipeline wiring (spans persisted from OCR lines with geometry).
"""
from datetime import date
from decimal import Decimal

import pytest
from django.conf import settings

from documents.models import MedicalDocument, MedicalDocumentEvent
from labs.models import LabReportExtraction, LabResult
from labs.parsing import Span, parse_page
from labs.services import process_lab_extraction
from processing.models import DocumentText, DocumentTextPage, DocumentTextSpan
from processing.ocr import OCRLine, OCRResult
from processing.ocr_services import process_ocr_document
from tests.test_ocr_processing import FakeEngine, make_document

pytestmark = pytest.mark.django_db

LAB_PIPELINE_VERSION = settings.LAB_PIPELINE_VERSION


def grid_spans(rows, *, page=1, confidence=0.95, x_units=0.25, y_step=0.12):
    """Build normalized Span objects from a text grid (synthetic only)."""
    spans = []
    sequence = 0
    for ri, row in enumerate(rows):
        for ci, cell in enumerate(row):
            if not cell:
                continue
            x0 = ci * x_units
            x1 = x0 + (x_units - 0.05)
            y0 = ri * y_step
            y1 = y0 + 0.08
            spans.append(
                Span(
                    page_number=page,
                    sequence=sequence,
                    text=cell,
                    confidence=confidence,
                    x_min=x0,
                    y_min=y0,
                    x_max=x1,
                    y_max=y1,
                )
            )
            sequence += 1
    return spans


CHEMISTRY_ROWS = [
    ["Item", "Result", "Unit", "Reference Range"],
    ["Chemistry"],
    ["Glucose", "92", "mg/dL", "70 - 99"],
    ["HbA1c", "5.4", "%", "Pre diabetic : 5.7-6.4"],
    ["Diabetic: > 6.5"],
    ["Creatinine", "1.0", "mg/dL", "0.7 - 1.18"],
]

HORMONE_ROWS = [
    ["Item", "Result", "Unit", "Reference Range"],
    ["Vitamins"],
    ["Vitamin D3", "19.3", "ng/mL", "Sufficiency 29 - 100"],
    ["Hormones"],
    ["Pituitary Hormones"],
    ["TSH", "1.24", "uIU/mL", "0.5 - 4.5"],
    ["Thyroid Hormones"],
    ["T4", "8.3", "ug/dL", "4.9 - 11.0"],
]

CBC_ROWS = [
    ["Test", "Result", "Flags", "Units", "Low", "High"],
    ["WBC", "6.71", "R", "x10^3/µL", "3.60", "10.20"],
    ["HGB", "14.68", "R", "g/dL", "12.50", "16.30"],
    ["PLT", "288.0", "R", "x10^3/µL", "150.0", "400.0"],
]


def grid_engine(rows, *, width=320, height=120, confidence=0.95):
    """Fake engine whose lines carry realistic grid geometry (one span per cell).

    Defaults match ``test_ocr_processing.make_document`` (320x120 image) so
    normalized span coordinates stay within 0.0-1.0.
    """
    lines = []
    flat = []
    x_units = width / max((len(row) for row in rows), default=4)
    y_step = height / max(len(rows), 1)
    for ri, row in enumerate(rows):
        for ci, cell in enumerate(row):
            if not cell:
                continue
            x0 = int(ci * x_units)
            y0 = int(ri * y_step)
            x1 = int(ci * x_units + x_units * 0.9)
            y1 = int(ri * y_step + y_step * 0.7)
            lines.append(OCRLine(cell, float(confidence), x0, y0, x1, y1))
            flat.append(cell)
    text = "\n".join(flat)
    result = OCRResult(
        text=text,
        lines=tuple(lines),
        mean_confidence=float(confidence),
        minimum_confidence=float(confidence),
        engine_name="fake-paddleocr",
        engine_version="3.7.0",
        duration_ms=12,
    )
    return FakeEngine([result])


# --------------------------------------------------------------------------- #
# Parser (pure) tests
# --------------------------------------------------------------------------- #


def test_chemistry_header_parsing():
    results = parse_page(1, grid_spans(CHEMISTRY_ROWS))
    assert [r.test_name_raw for r in results] == ["Glucose", "HbA1c", "Creatinine"]
    glucose = results[0]
    assert glucose.result_raw == "92"
    assert glucose.result_numeric == Decimal("92")
    assert glucose.unit_raw == "mg/dL"
    assert glucose.reference_low == Decimal("70")
    assert glucose.reference_high == Decimal("99")
    assert glucose.flag_raw == ""
    # wrapped multi-line reference merged
    hba1c = results[1]
    assert "Diabetic: > 6.5" in hba1c.reference_range_raw
    assert hba1c.reference_low is None  # complex range -> raw only


def test_hormone_report_sections_not_results():
    results = parse_page(1, grid_spans(HORMONE_ROWS))
    names = [r.test_name_raw for r in results]
    assert names == ["Vitamin D3", "TSH", "T4"]
    assert "Vitamins" not in names
    assert "Hormones" not in names
    assert "Pituitary Hormones" not in names
    assert "Thyroid Hormones" not in names


def test_cbc_low_high_columns():
    results = parse_page(1, grid_spans(CBC_ROWS))
    assert len(results) == 3
    wbc = results[0]
    assert wbc.test_name_raw == "WBC"
    assert wbc.result_raw == "6.71"
    assert wbc.flag_raw == "R"
    assert wbc.unit_raw == "x10^3/µL"
    assert wbc.reference_low == Decimal("3.60")
    assert wbc.reference_high == Decimal("10.20")


def test_two_tables_per_page():
    rows = [
        ["Test", "Result", "Units", "Low", "High"],
        ["WBC", "6.71", "x10^3/µL", "3.60", "10.20"],
        ["RBC", "5.44", "x10^3/µL", "4.06", "5.63"],
        ["Test", "Result", "Units", "Low", "High"],
        ["PLT", "288.0", "x10^3/µL", "150.0", "400.0"],
    ]
    results = parse_page(1, grid_spans(rows))
    assert [r.test_name_raw for r in results] == ["WBC", "RBC", "PLT"]


def test_radiology_narrative_zero_rows():
    rows = [
        ["ALWARKAA RADIOLOGY CENTER"],
        ["ABDOMINAL US"],
        ["Liver is of normal size", "showing normal texture", "no SOL."],
        ["Prostate measures 35 cc,", "PVRU is 10 cc."],
        ["Dr. Behjet Hani"],
    ]
    assert parse_page(1, grid_spans(rows)) == []


@pytest.mark.parametrize(
    "rows",
    [
        [["Patient ID", "303000"], ["Glucose", "92", "mg/dL", "70 - 99"]],
        [["Age/Sex", "45 Years/Male"], ["Glucose", "92", "mg/dL", "70 - 99"]],
        [["Requested", "30/6/2025 7:01PM"], ["Glucose", "92", "mg/dL", "70 - 99"]],
        [["Phone", "+9647701234567"], ["Glucose", "92", "mg/dL", "70 - 99"]],
        [["ISO 15189:2012"], ["Glucose", "92", "mg/dL", "70 - 99"]],
        [["Address", "Al-Kindi St, Baghdad"], ["Glucose", "92", "mg/dL", "70 - 99"]],
    ],
)
def test_false_positive_metadata_guards(rows):
    results = parse_page(1, grid_spans(rows))
    assert [r.test_name_raw for r in results] == ["Glucose"]


def test_ocr_variation_headers_still_detected():
    rows = [
        ["Test", "ResuIt", "Unlt", "ReferenceRange"],
        ["Glucose", "92", "mg/dL", "70 - 99"],
    ]
    results = parse_page(1, grid_spans(rows))
    assert len(results) == 1
    assert results[0].test_name_raw == "Glucose"
    assert results[0].result_raw == "92"
    assert results[0].unit_raw == "mg/dL"
    assert results[0].reference_low == Decimal("70")


def test_arabic_spans_preserved_no_false_rows():
    rows = [
        ["مختبر البيلسان"],
        ["Glucose", "92", "mg/dL", "70 - 99"],
        ["تاريخ: ٣٠/٦/٢٠٢٥"],
    ]
    results = parse_page(1, grid_spans(rows))
    assert [r.test_name_raw for r in results] == ["Glucose"]
    # Arabic spans are never corrupted by parsing
    arabic_texts = [s.text for s in grid_spans(rows) if any("\u0600" <= c <= "\u06FF" for c in s.text)]
    assert arabic_texts


def test_row_y_offsets_do_not_split_row():
    rows = [
        ["Item", "Result", "Unit", "Reference Range"],
        ["Glucose", "92", "mg/dL", "70 - 99"],
    ]
    spans = grid_spans(rows)
    # nudge the result cell Y by a small offset (same visual row)
    adjusted = []
    for span in spans:
        if span.text == "92":
            adjusted.append(
                Span(
                    span.page_number,
                    span.sequence,
                    span.text,
                    span.confidence,
                    span.x_min,
                    span.y_min + 0.02,
                    span.x_max,
                    span.y_max + 0.02,
                )
            )
        else:
            adjusted.append(span)
    results = parse_page(1, adjusted)
    assert len(results) == 1
    assert results[0].result_raw == "92"


# --------------------------------------------------------------------------- #
# Pipeline + DB roundtrip
# --------------------------------------------------------------------------- #


def test_pipeline_persists_spans_and_extracts(tmp_path):
    doc = make_document(tmp_path, mime_type="image/jpeg")
    doc.document_type = MedicalDocument.DocumentType.LABORATORY
    doc.save(update_fields=("document_type", "updated_at"))

    outcome = process_ocr_document(
        str(doc.uuid), engine=grid_engine(CHEMISTRY_ROWS)
    )

    doc.refresh_from_db()
    assert outcome == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
    page = doc.document_text.pages.get()
    spans = list(page.spans.order_by("sequence"))
    assert spans  # geometry persisted
    assert all(0.0 <= s.x_min <= s.x_max <= 1.0 for s in spans)
    assert all(0.0 <= s.y_min <= s.y_max <= 1.0 for s in spans)
    assert all(s.page_width and s.page_height for s in spans)

    # lab extraction consumes spans (no second OCR)
    process_lab_extraction(str(doc.uuid))
    extraction = LabReportExtraction.objects.get(document=doc)
    assert extraction.status == LabReportExtraction.Status.COMPLETED
    assert extraction.pipeline_version == LAB_PIPELINE_VERSION
    assert extraction.result_count >= 1
    rows = list(extraction.results.order_by("page_number", "row_index"))
    assert rows[0].test_name_raw == "Glucose"
    assert rows[0].result_numeric == Decimal("92")
    assert rows[0].unit_raw == "mg/dL"
    # evidence linked
    assert rows[0].source_spans.exists()


def test_db_roundtrip_exact(tmp_path):
    doc = make_document(tmp_path, mime_type="image/jpeg")
    doc.document_type = MedicalDocument.DocumentType.LABORATORY
    doc.save(update_fields=("document_type", "updated_at"))
    process_ocr_document(str(doc.uuid), engine=grid_engine(CHEMISTRY_ROWS))

    process_lab_extraction(str(doc.uuid))
    result = LabResult.objects.select_related("extraction").get(
        extraction__document=doc, test_name_raw="Creatinine"
    )
    assert result.test_name_raw == "Creatinine"
    assert result.result_raw == "1.0"
    assert result.result_numeric == Decimal("1.0")
    assert result.unit_raw == "mg/dL"
    assert result.reference_range_raw == "0.7 - 1.18"
    assert result.reference_low == Decimal("0.7")
    assert result.reference_high == Decimal("1.18")
    assert 0.0 <= result.extraction_confidence <= 1.0
    assert result.source_spans.count() >= 1


def test_roundtrip_special_result_not_numeric(tmp_path):
    doc = make_document(tmp_path, mime_type="image/jpeg")
    doc.document_type = MedicalDocument.DocumentType.LABORATORY
    doc.save(update_fields=("document_type", "updated_at"))
    rows = [
        ["Item", "Result", "Unit", "Reference Range"],
        ["Antibody", "Negative", "-", "-"],
    ]
    process_ocr_document(str(doc.uuid), engine=grid_engine(rows))
    process_lab_extraction(str(doc.uuid))
    result = LabResult.objects.get(extraction__document=doc)
    assert result.result_raw == "Negative"
    assert result.result_numeric is None
    assert result.result_text == "Negative"


def test_reprocess_idempotent(tmp_path):
    doc = make_document(tmp_path, mime_type="image/jpeg")
    doc.document_type = MedicalDocument.DocumentType.LABORATORY
    doc.save(update_fields=("document_type", "updated_at"))
    process_ocr_document(str(doc.uuid), engine=grid_engine(CHEMISTRY_ROWS))

    first = process_lab_extraction(str(doc.uuid))
    first_count = LabResult.objects.filter(extraction__document=doc).count()
    first_uuid = LabReportExtraction.objects.get(document=doc).uuid
    second = process_lab_extraction(str(doc.uuid))

    assert first == second == LabReportExtraction.Status.COMPLETED
    assert LabReportExtraction.objects.filter(document=doc).count() == 1
    assert LabReportExtraction.objects.get(document=doc).uuid == first_uuid
    assert LabResult.objects.filter(extraction__document=doc).count() == first_count


def test_parser_failure_non_fatal(tmp_path, monkeypatch):
    doc = make_document(tmp_path, mime_type="image/jpeg")
    doc.document_type = MedicalDocument.DocumentType.LABORATORY
    doc.save(update_fields=("document_type", "updated_at"))
    process_ocr_document(str(doc.uuid), engine=grid_engine(CHEMISTRY_ROWS))
    persisted_body = DocumentText.objects.get(document=doc).text

    def broken(*args, **kwargs):
        raise ValueError("private parser detail")

    outcome = process_lab_extraction(str(doc.uuid), parser=broken)

    doc.refresh_from_db()
    extraction = LabReportExtraction.objects.get(document=doc)
    assert outcome == LabReportExtraction.Status.FAILED
    assert extraction.status == LabReportExtraction.Status.FAILED
    assert extraction.error_code == "lab_parse_failed"
    assert not extraction.results.exists()  # no partial rows
    # archive-first: OCR body + document untouched
    assert doc.processing_status == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
    assert DocumentText.objects.get(document=doc).text == persisted_body
    assert doc.archive_status == MedicalDocument.ArchiveStatus.ACTIVE


def test_document_type_gate(tmp_path):
    doc = make_document(tmp_path, mime_type="image/jpeg")
    doc.document_type = MedicalDocument.DocumentType.RADIOLOGY
    doc.save(update_fields=("document_type", "updated_at"))
    process_ocr_document(str(doc.uuid), engine=grid_engine(CHEMISTRY_ROWS))

    outcome = process_lab_extraction(str(doc.uuid))

    assert outcome == LabReportExtraction.Status.NOT_APPLICABLE
    assert not LabReportExtraction.objects.filter(document=doc).exists()


def test_radiology_document_zero_rows(tmp_path):
    doc = make_document(tmp_path, mime_type="image/jpeg")
    doc.document_type = MedicalDocument.DocumentType.LABORATORY  # forced lab
    doc.save(update_fields=("document_type", "updated_at"))
    narrative = [
        ["ABDOMINAL US"],
        ["Liver is of normal size", "showing normal texture."],
        ["Prostate measures 35 cc."],
        ["Dr. Behjet Hani"],
    ]
    process_ocr_document(str(doc.uuid), engine=grid_engine(narrative))
    outcome = process_lab_extraction(str(doc.uuid))
    assert outcome == LabReportExtraction.Status.NOT_APPLICABLE
    assert LabReportExtraction.objects.get(document=doc).result_count == 0


def test_lab_values_not_in_events_or_logs(tmp_path, caplog):
    doc = make_document(tmp_path, mime_type="image/jpeg")
    doc.document_type = MedicalDocument.DocumentType.LABORATORY
    doc.save(update_fields=("document_type", "updated_at"))
    process_ocr_document(str(doc.uuid), engine=grid_engine(CHEMISTRY_ROWS))

    with caplog.at_level("INFO", logger="labs.services"):
        process_lab_extraction(str(doc.uuid))

    assert "Glucose" not in caplog.text
    assert "92" not in caplog.text
    event = MedicalDocumentEvent.objects.get(
        document=doc, event_type=MedicalDocumentEvent.EventType.LAB_EXTRACTION_COMPLETED
    )
    assert "result_count" in event.metadata
    assert "Glucose" not in str(event.metadata)
