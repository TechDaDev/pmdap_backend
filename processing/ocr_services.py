import logging
import math
import time
from dataclasses import dataclass
from datetime import timedelta

from billiard.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone

from audit.models import AuditLog
from audit.services import record_audit
from documents.models import MedicalDocument, MedicalDocumentEvent, StoredFile
from processing.extraction import PAGE_SEPARATOR, TextUsabilityEvaluator
from processing.models import DocumentText, DocumentTextPage, DocumentTextSpan
from processing.ocr import (
    ImagePreprocessor,
    OCREngineResultError,
    OCREngineUnavailableError,
    OCRError,
    OCRResult,
    OCRResultSizeError,
    PDFPageRenderer,
)
from processing.ocr_provider import get_ocr_engine
from processing.services import RetryablePDFProcessingError, _read_verified_content

logger = logging.getLogger(__name__)

IMAGE_MIME_TYPES = {"image/jpeg", "image/png"}


class RetryableOCRProcessingError(Exception):
    retryable = True

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OCRClaim:
    document: MedicalDocument
    source_text_uuid: str | None
    page_numbers: tuple[int, ...]


def _event(document, event_type, metadata=None):
    MedicalDocumentEvent.objects.create(
        document=document,
        event_type=event_type,
        actor=None,
        metadata=metadata or {},
    )


def _current_outcome(document):
    if not hasattr(document, "document_text"):
        return MedicalDocument.ProcessingStatus.UPLOADED
    return (
        MedicalDocument.ProcessingStatus.OCR_REQUIRED
        if document.document_text.has_pages_requiring_ocr
        and not document.document_text.usable
        else MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
    )


def _security_gate(document):
    return (
        document.archive_status == MedicalDocument.ArchiveStatus.ACTIVE
        and document.stored_file.integrity_status == StoredFile.IntegrityStatus.VALID
        and document.stored_file.malware_scan_status
        not in {
            StoredFile.MalwareScanStatus.INFECTED,
            StoredFile.MalwareScanStatus.ERROR,
        }
    )


def schedule_ocr(document, *, record_event=True):
    if record_event:
        _event(document, MedicalDocumentEvent.EventType.OCR_QUEUED)
    transaction.on_commit(lambda: _enqueue_ocr(str(document.uuid)))


def _enqueue_ocr(document_uuid):
    from processing.tasks import ocr_medical_document

    try:
        ocr_medical_document.delay(document_uuid)
    except Exception:
        logger.error(
            "OCR enqueue failed",
            extra={"document_uuid": document_uuid},
        )


def _claim(document_uuid):
    with transaction.atomic():
        document = (
            MedicalDocument.objects.select_for_update()
            .select_related("stored_file")
            .filter(uuid=document_uuid)
            .first()
        )
        if (
            document is None
            or document.archive_status != MedicalDocument.ArchiveStatus.ACTIVE
        ):
            return None, "SKIPPED"
        if not _security_gate(document):
            return None, _mark_failure(document_uuid, "medical_file_not_extractable")
        mime_type = document.stored_file.mime_type
        if mime_type == "application/pdf":
            if not hasattr(document, "document_text"):
                return None, "SKIPPED"
            pages = tuple(
                document.document_text.pages.filter(requires_ocr=True)
                .order_by("page_number")
                .values_list("page_number", flat=True)
            )
            if not pages:
                return None, _current_outcome(document)
            source_text_uuid = str(document.document_text.uuid)
        elif mime_type in IMAGE_MIME_TYPES:
            if hasattr(document, "document_text"):
                return None, _current_outcome(document)
            pages = (1,)
            source_text_uuid = None
        else:
            return None, "SKIPPED"

        if (
            document.processing_status
            == MedicalDocument.ProcessingStatus.OCR_PROCESSING
        ):
            stale_before = timezone.now() - timedelta(
                seconds=settings.OCR_TASK_TIME_LIMIT
            )
            if (
                document.processing_started_at is not None
                and document.processing_started_at > stale_before
            ):
                return None, MedicalDocument.ProcessingStatus.OCR_PROCESSING
        document.processing_status = MedicalDocument.ProcessingStatus.OCR_PROCESSING
        document.processing_failure_code = ""
        document.processing_started_at = timezone.now()
        document.save(
            update_fields=(
                "processing_status",
                "processing_failure_code",
                "processing_started_at",
                "updated_at",
            )
        )
        _event(
            document,
            MedicalDocumentEvent.EventType.OCR_STARTED,
            {"page_count": len(pages)},
        )
        return OCRClaim(document, source_text_uuid, pages), ""


def _mark_failure(document_uuid, code, *, retryable=False):
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
        outcome = _current_outcome(document)
        if not hasattr(document, "document_text") and not retryable:
            outcome = MedicalDocument.ProcessingStatus.FAILED
        canonical_complete = (
            hasattr(document, "document_text")
            and not document.document_text.has_pages_requiring_ocr
        )
        document.processing_status = outcome
        document.processing_failure_code = "" if canonical_complete else code
        document.processing_started_at = None
        document.save(
            update_fields=(
                "processing_status",
                "processing_failure_code",
                "processing_started_at",
                "updated_at",
            )
        )
        _event(
            document,
            MedicalDocumentEvent.EventType.OCR_FAILED,
            {
                "failure_code": code,
                "retryable": retryable,
                "canonical_preserved": hasattr(document, "document_text"),
            },
        )
        record_audit(
            action=AuditLog.Action.OCR_FAILED,
            actor_type=AuditLog.ActorType.SYSTEM,
            patient=document.patient,
            resource_type="MEDICAL_DOCUMENT",
            resource_uuid=document.uuid,
            new_values={"processing_status": document.processing_status},
            metadata={"failure_code": code, "retryable": retryable},
        )
    logger.warning(
        "OCR processing failed",
        extra={
            "document_uuid": str(document_uuid),
            "processing_status": str(outcome),
            "failure_code": code,
        },
    )
    return outcome


def _page_still_required(document_uuid, source_text_uuid, page_number):
    return DocumentTextPage.objects.filter(
        document_text__document__uuid=document_uuid,
        document_text__uuid=source_text_uuid,
        document_text__document__archive_status=MedicalDocument.ArchiveStatus.ACTIVE,
        page_number=page_number,
        requires_ocr=True,
    ).exists()


def _valid_result(result):
    if not isinstance(result, OCRResult):
        return False
    try:
        confidences_valid = all(
            value is None
            or (type(value) is float and math.isfinite(value) and 0 <= value <= 1)
            for value in (result.mean_confidence, result.minimum_confidence)
        )
        confidence_shape_valid = (
            bool(result.lines)
            and result.mean_confidence is not None
            and result.minimum_confidence is not None
        ) or (
            not result.lines
            and result.mean_confidence is None
            and result.minimum_confidence is None
        )
        return isinstance(result.text, str) and (
            isinstance(result.lines, tuple)
            and isinstance(result.engine_name, str)
            and 0 < len(result.engine_name) <= 64
            and isinstance(result.engine_version, str)
            and 0 < len(result.engine_version) <= 32
            and type(result.duration_ms) is int
            and result.duration_ms >= 0
            and isinstance(result.preprocessing_version, str)
            and 0 < len(result.preprocessing_version) <= 32
            and isinstance(result.pipeline_version, str)
            and 0 < len(result.pipeline_version) <= 32
            and all(
                isinstance(line.text, str)
                and type(line.confidence) is float
                and 0 <= line.confidence <= 1
                and _valid_line_geometry(line)
                for line in result.lines
            )
            and result.text == "\n".join(line.text for line in result.lines)
            and confidences_valid
            and confidence_shape_valid
        )
    except (AttributeError, TypeError):
        return False


def _meaningful(text):
    return TextUsabilityEvaluator.meaningful_character_count(text)


def _valid_line_geometry(line):
    coords = (line.x_min, line.y_min, line.x_max, line.y_max)
    if all(value is None for value in coords):
        return True
    if any(value is None for value in coords):
        return False
    return (
        all(type(value) is int and value >= 0 for value in coords)
        and line.x_min <= line.x_max
        and line.y_min <= line.y_max
    )


def _span_rows(page, width, height, lines):
    """Build normalized DocumentTextSpan rows from OCR lines with geometry."""
    if not width or not height:
        return []
    spans = []
    for index, line in enumerate(lines):
        if None in (line.x_min, line.y_min, line.x_max, line.y_max):
            continue
        spans.append(
            DocumentTextSpan(
                document_text_page=page,
                sequence=index,
                text=line.text,
                confidence=line.confidence,
                x_min=line.x_min / width,
                y_min=line.y_min / height,
                x_max=line.x_max / width,
                y_max=line.y_max / height,
                source=DocumentTextSpan.Source.OCR,
                page_width=width,
                page_height=height,
            )
        )
    return spans


def _persist_image_result(document, result, width, height):
    if _meaningful(result.text) < settings.OCR_TEXT_MIN_MEANINGFUL_CHARS:
        raise OCRError("OCR output did not contain usable text.")
    extracted = DocumentText.objects.create(
        document=document,
        text=result.text,
        page_count=1,
        character_count=len(result.text),
        meaningful_character_count=_meaningful(result.text),
        usable=True,
        usability_reason="usable_ocr_text",
        has_pages_requiring_ocr=False,
        extraction_method=DocumentText.ExtractionMethod.OCR,
        extractor_name=result.engine_name,
        extractor_version=result.engine_version,
        pipeline_version=result.pipeline_version,
        ocr_engine_name=result.engine_name,
        ocr_engine_version=result.engine_version,
        ocr_pipeline_version=result.pipeline_version,
    )
    page = DocumentTextPage.objects.create(
        document_text=extracted,
        page_number=1,
        text=result.text,
        native_text="",
        ocr_text=result.text,
        meaningful_character_count=_meaningful(result.text),
        requires_ocr=False,
        ocr_completed=True,
        effective_source=DocumentTextPage.EffectiveSource.OCR,
        ocr_engine_name=result.engine_name,
        ocr_engine_version=result.engine_version,
        ocr_mean_confidence=result.mean_confidence,
        ocr_minimum_confidence=result.minimum_confidence,
        ocr_duration_ms=result.duration_ms,
        preprocessing_version=result.preprocessing_version,
    )
    DocumentTextSpan.objects.bulk_create(
        _span_rows(page, width, height, result.lines),
        ignore_conflicts=True,
    )
    return extracted


def _persist_pdf_results(document, source_text_uuid, results, page_dims):
    if str(document.document_text.uuid) != source_text_uuid:
        return None
    pages = {
        page.page_number: page
        for page in document.document_text.pages.select_for_update().filter(
            page_number__in=results
        )
    }
    if set(pages) != set(results) or any(
        not page.requires_ocr for page in pages.values()
    ):
        return None
    for page_number, result in results.items():
        if _meaningful(result.text) < settings.OCR_TEXT_MIN_MEANINGFUL_CHARS:
            raise OCRError("OCR output did not contain usable text.")
        page = pages[page_number]
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
        if page_dims:
            width, height = page_dims.get(page_number, (0, 0))
            if width and height:
                DocumentTextSpan.objects.filter(document_text_page=page).delete()
                DocumentTextSpan.objects.bulk_create(
                    _span_rows(page, width, height, result.lines),
                    ignore_conflicts=True,
                )
        _event(
            document,
            MedicalDocumentEvent.EventType.OCR_PAGE_COMPLETED,
            {
                "page_number": page_number,
                "engine_name": result.engine_name,
                "duration_ms": result.duration_ms,
                "character_count": len(result.text),
                "mean_confidence": result.mean_confidence,
            },
        )
    extracted = document.document_text
    ordered_pages = tuple(extracted.pages.order_by("page_number"))
    aggregate = PAGE_SEPARATOR.join(page.text for page in ordered_pages)
    if len(aggregate) > settings.OCR_MAX_TEXT_CHARS_PER_DOCUMENT:
        raise OCRResultSizeError("OCR text exceeds the document limit.")
    extracted.text = aggregate
    extracted.character_count = len(aggregate)
    extracted.meaningful_character_count = sum(
        page.meaningful_character_count for page in ordered_pages
    )
    extracted.has_pages_requiring_ocr = any(page.requires_ocr for page in ordered_pages)
    extracted.usable = not extracted.has_pages_requiring_ocr
    extracted.usability_reason = (
        "usable_ocr_text" if extracted.usable else "insufficient_meaningful_text"
    )
    has_native = any(
        page.effective_source == DocumentTextPage.EffectiveSource.PDF_TEXT
        for page in ordered_pages
    )
    extracted.extraction_method = (
        DocumentText.ExtractionMethod.HYBRID
        if has_native
        else DocumentText.ExtractionMethod.OCR
    )
    first_result = next(iter(results.values()))
    extracted.ocr_engine_name = first_result.engine_name
    extracted.ocr_engine_version = first_result.engine_version
    extracted.ocr_pipeline_version = first_result.pipeline_version
    extracted.save()
    return extracted


def _persist(document_uuid, source_text_uuid, results, page_dims):
    try:
        with transaction.atomic():
            document = (
                MedicalDocument.objects.select_for_update()
                .select_related("stored_file")
                .filter(uuid=document_uuid)
                .first()
            )
            if document is None or not _security_gate(document):
                return "SKIPPED"
            if any(not _valid_result(result) for result in results.values()):
                return _mark_failure(document_uuid, "ocr_malformed_result")
            if source_text_uuid is None:
                if hasattr(document, "document_text"):
                    return _current_outcome(document)
                width, height = page_dims.get(1, (0, 0))
                extracted = _persist_image_result(document, results[1], width, height)
            else:
                extracted = _persist_pdf_results(document, source_text_uuid, results, page_dims)
                if extracted is None:
                    return _current_outcome(document)
            document.processing_status = (
                MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
                if extracted.usable
                else MedicalDocument.ProcessingStatus.OCR_REQUIRED
            )
            document.processing_failure_code = ""
            document.processing_started_at = None
            document.save(
                update_fields=(
                    "processing_status",
                    "processing_failure_code",
                    "processing_started_at",
                    "updated_at",
                )
            )
            _event(
                document,
                MedicalDocumentEvent.EventType.OCR_COMPLETED,
                {
                    "page_count": len(results),
                    "engine_name": extracted.ocr_engine_name,
                    "character_count": extracted.character_count,
                },
            )
            if (
                document.processing_status
                == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
            ):
                from processing.date_services import schedule_date_processing

                schedule_date_processing(document)
                if (
                    document.document_type
                    == MedicalDocument.DocumentType.LABORATORY
                ):
                    from labs.services import schedule_lab_extraction

                    schedule_lab_extraction(document)
            return document.processing_status
    except OCRError as exc:
        return _mark_failure(document_uuid, exc.code)
    except DatabaseError:
        return _mark_failure(document_uuid, "ocr_persistence_failed")


def process_ocr_document(
    document_uuid,
    *,
    engine=None,
    preprocessor=None,
    renderer=None,
):
    started_at = time.monotonic()
    claim, outcome = _claim(document_uuid)
    if claim is None:
        return outcome
    try:
        try:
            content, failure_code = _read_verified_content(claim.document.stored_file)
        except RetryablePDFProcessingError as exc:
            raise RetryableOCRProcessingError(exc.code) from exc
        if failure_code:
            return _mark_failure(document_uuid, failure_code)
        if engine is None:
            if settings.OCR_ENGINE != "paddleocr":
                raise OCREngineUnavailableError("Configured OCR engine is unavailable.")
            engine = get_ocr_engine()
        preprocessor = preprocessor or ImagePreprocessor()
        renderer = renderer or PDFPageRenderer()
        results = {}
        page_dims = {}
        if claim.document.stored_file.mime_type in IMAGE_MIME_TYPES:
            image = preprocessor.prepare(content)
            try:
                page_dims[1] = image.size
                results[1] = engine.extract_image(image)
            finally:
                image.close()
        else:
            for page_number in claim.page_numbers:
                if not _page_still_required(
                    document_uuid, claim.source_text_uuid, page_number
                ):
                    continue
                image = renderer.render(content, page_number)
                try:
                    page_dims[page_number] = image.size
                    results[page_number] = engine.extract_image(image)
                finally:
                    image.close()
        if not results:
            return _current_outcome(claim.document)
        if any(not _valid_result(result) for result in results.values()):
            raise OCREngineResultError("OCR engine returned malformed output.")
        if any(
            len(result.text) > settings.OCR_MAX_TEXT_CHARS_PER_PAGE
            for result in results.values()
        ):
            raise OCRResultSizeError("OCR text exceeds the per-page limit.")
        total_characters = sum(len(result.text) for result in results.values())
        if total_characters > settings.OCR_MAX_TEXT_CHARS_PER_DOCUMENT:
            raise OCRResultSizeError("OCR text exceeds the document limit.")
    except RetryableOCRProcessingError as exc:
        _mark_failure(document_uuid, exc.code, retryable=True)
        raise
    except OCRError as exc:
        if not exc.retryable:
            return _mark_failure(document_uuid, exc.code)
        _mark_failure(document_uuid, exc.code, retryable=True)
        raise RetryableOCRProcessingError(exc.code) from exc
    except (MemoryError, OSError, SoftTimeLimitExceeded) as exc:
        _mark_failure(document_uuid, "ocr_resource_retryable", retryable=True)
        raise RetryableOCRProcessingError("ocr_resource_retryable") from exc
    except Exception:
        return _mark_failure(document_uuid, "ocr_failed")

    outcome = _persist(document_uuid, claim.source_text_uuid, results, page_dims)
    if outcome == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED:
        logger.info(
            "OCR processing completed",
            extra={
                "document_uuid": str(document_uuid),
                "processing_status": str(outcome),
                "page_count": len(results),
                "duration_ms": int((time.monotonic() - started_at) * 1000),
            },
        )
    return outcome
