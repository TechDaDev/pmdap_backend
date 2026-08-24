"""Structured lab extraction orchestration.

Runs after OCR has persisted canonical text + spans (never a second OCR).
Failure is non-fatal: the archived document and OCR body are never invalidated;
the outcome (including failure) is recorded on ``LabReportExtraction``.
"""
import logging
import re
import time

from django.conf import settings
from django.db import DatabaseError, transaction

from documents.models import MedicalDocument, MedicalDocumentEvent
from labs.models import LabReportExtraction, LabResult
from labs.parsing import Span, parse_page

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Conservative normalization (raw fields stay authoritative)
# --------------------------------------------------------------------------- #

NORMALIZED_ALIASES = {
    "creatinine": "CREATININE",
    "serum creatinine": "CREATININE",
    "urea": "UREA",
    "blood urea": "UREA",
    "sodium": "SODIUM",
    "potassium": "POTASSIUM",
    "chloride": "CHLORIDE",
    "calcium": "CALCIUM",
    "total cholesterol": "TOTAL_CHOLESTEROL",
    "cholesterol": "TOTAL_CHOLESTEROL",
    "hdl cholesterol": "HDL_CHOLESTEROL",
    "hdl": "HDL_CHOLESTEROL",
    "ldl cholesterol": "LDL_CHOLESTEROL",
    "ldl": "LDL_CHOLESTEROL",
    "triglycerides": "TRIGLYCERIDES",
    "glucose": "GLUCOSE",
    "fasting blood sugar": "GLUCOSE",
    "hba1c": "HBA1C",
    "hemoglobin a1c": "HBA1C",
    "wbc": "WBC",
    "white blood cells": "WBC",
    "rbc": "RBC",
    "red blood cells": "RBC",
    "hemoglobin": "HEMOGLOBIN",
    "hematocrit": "HEMATOCRIT",
    "platelets": "PLATELETS",
    "tsh": "TSH",
    "t3": "T3",
    "t4": "T4",
    "vitamin d3": "VITAMIN_D3",
    "vitamin d": "VITAMIN_D3",
    "psa": "PSA",
    "uric acid": "URIC_ACID",
    "total bilirubin": "TOTAL_BILIRUBIN",
    "bilirubin": "TOTAL_BILIRUBIN",
    "alanine aminotransferase": "ALT",
    "aspartate aminotransferase": "AST",
    "alkaline phosphatase": "ALKALINE_PHOSPHATASE",
}

UNIT_ALIASES = {
    "mg/dl": "mg/dL",
    "mg/ml": "mg/mL",
    "ng/ml": "ng/mL",
    "ng/dl": "ng/dL",
    "g/dl": "g/dL",
    "g/l": "g/L",
    "ug/dl": "µg/dL",
    "ug/ml": "µg/mL",
    "pg/ml": "pg/mL",
    "u/l": "U/L",
    "iu/l": "IU/L",
    "muiu/ml": "mIU/mL",
    "uiu/ml": "mIU/mL",
    "uiu/l": "mIU/L",
    "miu/l": "mIU/L",
    "%": "%",
    "µl": "µL",
    "ul": "µL",
    "fl": "fL",
    "mmol/l": "mmol/L",
    "umol/l": "µmol/L",
    "x10^3/µl": "x10^3/µL",
    "x103/µl": "x10^3/µL",
    "x10^3/ul": "x10^3/µL",
}


def normalize_test_name(raw: str) -> str:
    key = raw.strip().lower()
    key = key.rstrip("*").strip()
    key = re.sub(r"[^a-z0-9 ]+", " ", key)
    key = re.sub(r"\s+", " ", key).strip()
    if key.startswith("s "):
        key = key[2:].strip()
    return NORMALIZED_ALIASES.get(key, "")


def normalize_unit(raw: str) -> str:
    key = raw.strip().lower().replace(" ", "")
    return UNIT_ALIASES.get(key, "")


# --------------------------------------------------------------------------- #
# Events / scheduling
# --------------------------------------------------------------------------- #


def _event(document, event_type, metadata=None):
    MedicalDocumentEvent.objects.create(
        document=document,
        event_type=event_type,
        actor=None,
        metadata=metadata or {},
    )


def schedule_lab_extraction(document):
    _event(document, MedicalDocumentEvent.EventType.LAB_EXTRACTION_QUEUED)
    transaction.on_commit(lambda: _enqueue_lab_extraction(str(document.uuid)))


def _enqueue_lab_extraction(document_uuid):
    from labs.tasks import extract_lab_results

    try:
        extract_lab_results.delay(document_uuid)
    except Exception:
        logger.error(
            "Lab extraction enqueue failed",
            extra={"document_uuid": document_uuid},
        )


def _snapshot_spans(document):
    """Load normalized spans from DB (no OCR, no second pass)."""
    rows = []
    pages = document.document_text.pages.order_by("page_number")
    for page in pages:
        rows.append(
            (
                page.page_number,
                [
                    Span(
                        page_number=page.page_number,
                        sequence=span.sequence,
                        text=span.text,
                        confidence=span.confidence,
                        x_min=span.x_min,
                        y_min=span.y_min,
                        x_max=span.x_max,
                        y_max=span.y_max,
                    )
                    for span in page.spans.order_by("sequence")
                ],
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


def process_lab_extraction(document_uuid, *, parser=parse_page):
    """Run (or re-run) structured lab extraction for one document.

    Idempotent for the current pipeline version: the previous extraction is
    replaced atomically. Never fails the MedicalDocument.
    """
    started = time.monotonic()
    try:
        with transaction.atomic():
            document = (
                MedicalDocument.objects.select_for_update()
                .filter(uuid=document_uuid)
                .first()
            )
            if (
                document is None
                or document.archive_status != MedicalDocument.ArchiveStatus.ACTIVE
            ):
                return "SKIPPED"
            if document.document_type != MedicalDocument.DocumentType.LABORATORY:
                return LabReportExtraction.Status.NOT_APPLICABLE
            if not hasattr(document, "document_text"):
                return "SKIPPED"
    except DatabaseError:
        return "SKIPPED"

    try:
        parsed_rows = []
        for page_number, spans in _snapshot_spans(document):
            parsed_rows.extend(parser(page_number, spans))
    except Exception:
        return _mark_failure(document)

    return _persist(document, parsed_rows, elapsed_ms=int((time.monotonic() - started) * 1000))


def _persist(document, parsed_rows, *, elapsed_ms):
    try:
        with transaction.atomic():
            extraction, _ = LabReportExtraction.objects.get_or_create(
                document=document,
                pipeline_version=settings.LAB_PIPELINE_VERSION,
            )
            extraction.results.all().delete()
            created = []
            for row in parsed_rows:
                result = LabResult(
                    extraction=extraction,
                    page_number=row.page_number,
                    row_index=row.row_index,
                    test_name_raw=row.test_name_raw,
                    test_name_normalized=normalize_test_name(row.test_name_raw),
                    result_raw=row.result_raw,
                    result_numeric=row.result_numeric,
                    result_text=row.result_text,
                    unit_raw=row.unit_raw,
                    unit_normalized=normalize_unit(row.unit_raw),
                    reference_range_raw=row.reference_range_raw,
                    reference_low=row.reference_low,
                    reference_high=row.reference_high,
                    flag_raw=row.flag_raw,
                    extraction_confidence=row.extraction_confidence,
                )
                result.save()
                created.append((result, row))
            for result, row in created:
                span_ids = _evidence_span_ids(document, row)
                if span_ids:
                    result.source_spans.set(span_ids)
            confidences = [row.extraction_confidence for row in parsed_rows]
            extraction.status = (
                LabReportExtraction.Status.COMPLETED
                if parsed_rows
                else LabReportExtraction.Status.NOT_APPLICABLE
            )
            extraction.result_count = len(parsed_rows)
            extraction.extraction_confidence = (
                sum(confidences) / len(confidences) if confidences else None
            )
            extraction.error_code = ""
            extraction.save(
                update_fields=(
                    "status",
                    "result_count",
                    "extraction_confidence",
                    "error_code",
                    "updated_at",
                )
            )
    except DatabaseError:
        return _mark_failure(document)
    except Exception:
        return _mark_failure(document)

    from documents.page_services import sync_document_to_page_units

    sync_document_to_page_units(document)

    event_type = (
        MedicalDocumentEvent.EventType.LAB_EXTRACTION_COMPLETED
        if parsed_rows
        else MedicalDocumentEvent.EventType.LAB_EXTRACTION_NOT_APPLICABLE
    )
    _event(
        document,
        event_type,
        {
            "pipeline_version": settings.LAB_PIPELINE_VERSION,
            "result_count": len(parsed_rows),
            "elapsed_ms": elapsed_ms,
        },
    )
    logger.info(
        "Lab extraction completed",
        extra={
            "document_uuid": str(document.uuid),
            "pipeline_version": settings.LAB_PIPELINE_VERSION,
            "result_count": len(parsed_rows),
            "status": (
                LabReportExtraction.Status.COMPLETED
                if parsed_rows
                else LabReportExtraction.Status.NOT_APPLICABLE
            ),
            "elapsed_ms": elapsed_ms,
        },
    )
    return (
        LabReportExtraction.Status.COMPLETED
        if parsed_rows
        else LabReportExtraction.Status.NOT_APPLICABLE
    )


def _evidence_span_ids(document, row):
    from processing.models import DocumentTextSpan

    return DocumentTextSpan.objects.filter(
        document_text_page__document_text__document=document,
        document_text_page__page_number=row.page_number,
        sequence__in=row.evidence_sequence,
    )


# --------------------------------------------------------------------------- #
# Page-scoped extraction (multi-page PDFs)
# --------------------------------------------------------------------------- #


def schedule_page_lab_extraction(page_unit):
    _event(
        page_unit.document,
        MedicalDocumentEvent.EventType.LAB_EXTRACTION_QUEUED,
        {"page_number": page_unit.page_number},
    )
    transaction.on_commit(
        lambda: _enqueue_page_lab_extraction(str(page_unit.uuid))
    )


def _enqueue_page_lab_extraction(page_unit_uuid):
    from labs.tasks import extract_page_lab_results

    try:
        extract_page_lab_results.delay(page_unit_uuid)
    except Exception:
        logger.error(
            "Page lab extraction enqueue failed",
            extra={"page_unit_uuid": page_unit_uuid},
        )


def process_lab_extraction_for_page(
    page_unit_uuid, *, parser=parse_page, classifier=None
):
    """Run structured lab extraction for ONE report page unit.

    Input: persisted spans for exactly that page. Output: one page-scoped
    LabReportExtraction + LabResult rows. Never queries other pages. A page
    parse failure fails only this page.
    """
    from documents.models import MedicalDocumentPage
    from documents.page_services import (
        detect_report_subtype,
        _finalize_page,
    )

    started = time.monotonic()
    try:
        with transaction.atomic():
            page = (
                MedicalDocumentPage.objects.select_for_update()
                .select_related("document")
                .filter(uuid=page_unit_uuid)
                .first()
            )
            if page is None or (
                page.document.archive_status
                != MedicalDocument.ArchiveStatus.ACTIVE
            ):
                return "SKIPPED"
            document = page.document
            if document.document_type != MedicalDocument.DocumentType.LABORATORY:
                _finalize_page(page)
                return LabReportExtraction.Status.NOT_APPLICABLE
    except DatabaseError:
        return "SKIPPED"

    try:
        spans = _page_spans(document, page.page_number)
        # Scanned PDFs can carry a garbage embedded text layer that passes the
        # usability check but yields NO OCR spans -> structured parsing needs
        # geometry evidence. Run the page's first-pass OCR lazily (no-op when
        # spans already exist); per-page isolated, never a second pass.
        if not spans and hasattr(document, "document_text"):
            _ensure_page_ocr(document, page.page_number)
            spans = _page_spans(document, page.page_number)
        if classifier is None:
            classifier = detect_report_subtype
        source_page = (
            document.document_text.pages.filter(
                page_number=page.page_number
            ).first()
            if hasattr(document, "document_text")
            else None
        )
        page_text = source_page.text if source_page is not None else ""
        page.report_subtype = classifier(
            "\n".join(span.text for span in spans) if spans else page_text
        )
        page.save(update_fields=("report_subtype", "updated_at"))
        parsed_rows = parser(page.page_number, spans)
    except Exception:
        return _mark_page_failure(page)

    return _persist_page(
        page, parsed_rows, elapsed_ms=int((time.monotonic() - started) * 1000)
    )


def _page_spans(document, page_number):
    from labs.parsing import Span

    page = (
        document.document_text.pages.filter(page_number=page_number).first()
        if hasattr(document, "document_text")
        else None
    )
    if page is None:
        return []
    return [
        Span(
            page_number=page.page_number,
            sequence=span.sequence,
            text=span.text,
            confidence=span.confidence,
            x_min=span.x_min,
            y_min=span.y_min,
            x_max=span.x_max,
            y_max=span.y_max,
        )
        for span in page.spans.order_by("sequence")
    ]


def _ensure_page_ocr(document, page_number):
    """First-pass OCR for ONE page that has native text but no spans.

    Scanned PDFs often embed a garbage text layer that passes the char-count
    usability check, so the normal OCR step is skipped and no spans exist.
    Structured lab parsing requires span geometry, so OCR this page lazily.
    No-op when the page already has spans (never a second OCR).
    """
    from processing.models import DocumentTextPage, DocumentTextSpan
    from processing.ocr import PDFPageRenderer
    from processing.ocr_services import _meaningful, _span_rows
    from processing.ocr_provider import get_ocr_engine
    from processing.services import _read_verified_content

    if not hasattr(document, "document_text"):
        return None
    page = (
        document.document_text.pages.filter(page_number=page_number).first()
    )
    if page is None or page.spans.exists() or page.ocr_completed:
        return page
    try:
        content, _ = _read_verified_content(document.stored_file)
        renderer = PDFPageRenderer()
        engine = get_ocr_engine()
        image = renderer.render(content, page_number)
        try:
            size = image.size
            result = engine.extract_image(image)
        finally:
            image.close()
        width, height = size
        page.ocr_text = result.text
        page.text = result.text
        page.meaningful_character_count = _meaningful(result.text)
        page.requires_ocr = False
        page.ocr_completed = True
        page.effective_source = DocumentTextPage.EffectiveSource.OCR
        page.ocr_engine_name = result.engine_name
        page.ocr_engine_version = result.engine_version
        page.ocr_mean_confidence = result.mean_confidence
        page.ocr_minimum_confidence = result.minimum_confidence
        page.ocr_duration_ms = result.duration_ms
        page.preprocessing_version = result.preprocessing_version
        page.save()
        if width and height:
            DocumentTextSpan.objects.filter(document_text_page=page).delete()
            DocumentTextSpan.objects.bulk_create(
                _span_rows(page, width, height, result.lines),
                ignore_conflicts=True,
            )
    except Exception:  # noqa: BLE001 - OCR failure isolates to this page
        logger.warning(
            "Page OCR (lazy) failed",
            extra={
                "document_uuid": str(document.uuid),
                "page_number": page_number,
            },
        )
    return page


def _persist_page(page, parsed_rows, *, elapsed_ms):
    from documents.page_services import _finalize_page

    try:
        with transaction.atomic():
            extraction, _ = LabReportExtraction.objects.get_or_create(
                document=page.document,
                page_unit=page,
                pipeline_version=settings.LAB_PIPELINE_VERSION,
            )
            extraction.results.all().delete()
            created = []
            for row in parsed_rows:
                result = LabResult(
                    extraction=extraction,
                    page_number=row.page_number,
                    row_index=row.row_index,
                    test_name_raw=row.test_name_raw,
                    test_name_normalized=normalize_test_name(row.test_name_raw),
                    result_raw=row.result_raw,
                    result_numeric=row.result_numeric,
                    result_text=row.result_text,
                    unit_raw=row.unit_raw,
                    unit_normalized=normalize_unit(row.unit_raw),
                    reference_range_raw=row.reference_range_raw,
                    reference_low=row.reference_low,
                    reference_high=row.reference_high,
                    flag_raw=row.flag_raw,
                    extraction_confidence=row.extraction_confidence,
                )
                result.save()
                created.append((result, row))
            for result, row in created:
                span_ids = _page_evidence_span_ids(page, row)
                if span_ids:
                    result.source_spans.set(span_ids)
            confidences = [row.extraction_confidence for row in parsed_rows]
            extraction.status = (
                LabReportExtraction.Status.COMPLETED
                if parsed_rows
                else LabReportExtraction.Status.NOT_APPLICABLE
            )
            extraction.result_count = len(parsed_rows)
            extraction.extraction_confidence = (
                sum(confidences) / len(confidences) if confidences else None
            )
            extraction.error_code = ""
            extraction.save(
                update_fields=(
                    "status",
                    "result_count",
                    "extraction_confidence",
                    "error_code",
                    "updated_at",
                )
            )
    except DatabaseError:
        return _mark_page_failure(page)
    except Exception:
        return _mark_page_failure(page)

    event_type = (
        MedicalDocumentEvent.EventType.LAB_EXTRACTION_COMPLETED
        if parsed_rows
        else MedicalDocumentEvent.EventType.LAB_EXTRACTION_NOT_APPLICABLE
    )
    _event(
        page.document,
        event_type,
        {
            "page_number": page.page_number,
            "pipeline_version": settings.LAB_PIPELINE_VERSION,
            "result_count": len(parsed_rows),
            "elapsed_ms": elapsed_ms,
        },
    )
    logger.info(
        "Page lab extraction completed",
        extra={
            "document_uuid": str(page.document.uuid),
            "page_number": page.page_number,
            "pipeline_version": settings.LAB_PIPELINE_VERSION,
            "result_count": len(parsed_rows),
        },
    )
    _finalize_page(page)
    return (
        LabReportExtraction.Status.COMPLETED
        if parsed_rows
        else LabReportExtraction.Status.NOT_APPLICABLE
    )


def _page_evidence_span_ids(page, row):
    from processing.models import DocumentTextSpan

    return DocumentTextSpan.objects.filter(
        document_text_page__document_text__document=page.document,
        document_text_page__page_number=page.page_number,
        sequence__in=row.evidence_sequence,
    )


def _mark_page_failure(page):
    try:
        with transaction.atomic():
            extraction, _ = LabReportExtraction.objects.get_or_create(
                document=page.document,
                page_unit=page,
                pipeline_version=settings.LAB_PIPELINE_VERSION,
            )
            extraction.results.all().delete()
            extraction.status = LabReportExtraction.Status.FAILED
            extraction.error_code = "lab_parse_failed"
            extraction.result_count = 0
            extraction.extraction_confidence = None
            extraction.save(
                update_fields=(
                    "status",
                    "error_code",
                    "result_count",
                    "extraction_confidence",
                    "updated_at",
                )
            )
    except DatabaseError:
        pass
    from documents.page_services import _finalize_page

    _finalize_page(page)
    return LabReportExtraction.Status.FAILED


def _mark_failure(document):
    try:
        with transaction.atomic():
            extraction, _ = LabReportExtraction.objects.get_or_create(
                document=document,
                pipeline_version=settings.LAB_PIPELINE_VERSION,
            )
            extraction.results.all().delete()
            extraction.status = LabReportExtraction.Status.FAILED
            extraction.error_code = "lab_parse_failed"
            extraction.result_count = 0
            extraction.extraction_confidence = None
            extraction.save(
                update_fields=(
                    "status",
                    "error_code",
                    "result_count",
                    "extraction_confidence",
                    "updated_at",
                )
            )
    except Exception:
        logger.error(
            "Lab extraction failure recording failed",
            extra={"document_uuid": str(document.uuid)},
        )
    _event(
        document,
        MedicalDocumentEvent.EventType.LAB_EXTRACTION_FAILED,
        {"pipeline_version": settings.LAB_PIPELINE_VERSION},
    )
    logger.warning(
        "Lab extraction failed",
        extra={"document_uuid": str(document.uuid)},
    )
    return LabReportExtraction.Status.FAILED
