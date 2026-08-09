import hashlib
import logging
import time
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone

from documents.models import MedicalDocument, MedicalDocumentEvent, StoredFile
from processing.extraction import PDFExtractionError, PDFTextExtractor, PDFTextResult
from processing.models import DocumentText, DocumentTextPage

logger = logging.getLogger(__name__)


class RetryablePDFProcessingError(Exception):
    retryable = True

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _event(document, event_type, metadata=None):
    MedicalDocumentEvent.objects.create(
        document=document,
        event_type=event_type,
        actor=None,
        metadata=metadata or {},
    )


def _canonical_outcome(document_text):
    return (
        MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
        if document_text.usable
        else MedicalDocument.ProcessingStatus.OCR_REQUIRED
    )


def _mark_failure(document_uuid, code, *, retryable=False):
    with transaction.atomic():
        document = (
            MedicalDocument.objects.select_for_update()
            .filter(uuid=document_uuid)
            .first()
        )
        if document is None or (
            document.archive_status != MedicalDocument.ArchiveStatus.ACTIVE
        ):
            return "SKIPPED"
        if hasattr(document, "document_text"):
            outcome = _canonical_outcome(document.document_text)
            document.processing_status = outcome
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
                MedicalDocumentEvent.EventType.PDF_EXTRACTION_FAILED,
                {
                    "failure_code": code,
                    "retryable": retryable,
                    "canonical_preserved": True,
                },
            )
            return outcome
        document.processing_status = (
            MedicalDocument.ProcessingStatus.QUEUED
            if retryable
            else MedicalDocument.ProcessingStatus.FAILED
        )
        document.processing_failure_code = code
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
            MedicalDocumentEvent.EventType.PDF_EXTRACTION_FAILED,
            {"failure_code": code, "retryable": retryable},
        )
    logger.warning(
        "PDF extraction failed",
        extra={
            "document_uuid": str(document_uuid),
            "processing_status": document.processing_status,
            "failure_code": code,
        },
    )
    return document.processing_status


def _read_verified_content(stored_file):
    try:
        with stored_file.file.open("rb") as source:
            content = source.read(stored_file.size_bytes + 1)
    except FileNotFoundError:
        return None, "medical_file_missing"
    except OSError as exc:
        raise RetryablePDFProcessingError("medical_file_read_retryable") from exc

    digest = hashlib.sha256(content).hexdigest()
    if len(content) != stored_file.size_bytes or digest != stored_file.sha256:
        stored_file.integrity_status = StoredFile.IntegrityStatus.CORRUPTED
        stored_file.save(update_fields=("integrity_status", "updated_at"))
        return None, "medical_file_integrity_mismatch"
    return content, ""


def _valid_result(result, stored_file):
    if not isinstance(result, PDFTextResult):
        return False
    try:
        pages = tuple(result.pages)
        metadata = result.metadata
        expected_ocr_pages = [page.page_number for page in pages if page.requires_ocr]
        return (
            isinstance(result.text, str)
            and type(result.page_count) is int
            and result.page_count >= 0
            and type(result.character_count) is int
            and result.character_count >= 0
            and type(result.usable) is bool
            and isinstance(result.reason, str)
            and 0 < len(result.reason) <= 64
            and result.page_count == len(pages)
            and result.page_count == stored_file.page_count
            and result.character_count == len(result.text)
            and [page.page_number for page in pages]
            == list(range(1, result.page_count + 1))
            and metadata["extraction_method"] == "PDF_TEXT"
            and isinstance(metadata["extractor_name"], str)
            and 0 < len(metadata["extractor_name"]) <= 64
            and isinstance(metadata["extractor_version"], str)
            and 0 < len(metadata["extractor_version"]) <= 32
            and isinstance(metadata["pipeline_version"], str)
            and 0 < len(metadata["pipeline_version"]) <= 32
            and type(metadata["meaningful_character_count"]) is int
            and metadata["meaningful_character_count"] >= 0
            and metadata["meaningful_character_count"]
            == sum(page.meaningful_character_count for page in pages)
            and metadata["pages_requiring_ocr"] == expected_ocr_pages
            and all(
                isinstance(page.text, str)
                and type(page.page_number) is int
                and type(page.meaningful_character_count) is int
                and page.meaningful_character_count >= 0
                and type(page.requires_ocr) is bool
                for page in pages
            )
        )
    except (AttributeError, KeyError, TypeError):
        return False


def _claim(document_uuid, *, reprocess=False):
    with transaction.atomic():
        document = (
            MedicalDocument.objects.select_for_update()
            .select_related("stored_file")
            .filter(uuid=document_uuid)
            .first()
        )
        if document is None:
            return None, "SKIPPED"
        if document.archive_status != MedicalDocument.ArchiveStatus.ACTIVE:
            return None, "SKIPPED"
        if document.stored_file.mime_type != "application/pdf":
            return None, "SKIPPED"
        if document.stored_file.integrity_status != StoredFile.IntegrityStatus.VALID:
            return None, _mark_failure(document_uuid, "medical_file_not_extractable")
        if document.stored_file.malware_scan_status in {
            StoredFile.MalwareScanStatus.INFECTED,
            StoredFile.MalwareScanStatus.ERROR,
        }:
            return None, _mark_failure(document_uuid, "medical_file_not_extractable")
        if hasattr(document, "document_text") and not reprocess:
            return None, _canonical_outcome(document.document_text)
        if document.processing_status == MedicalDocument.ProcessingStatus.PROCESSING:
            stale_before = timezone.now() - timedelta(
                seconds=settings.PDF_EXTRACTION_TIME_LIMIT
            )
            if (
                document.processing_started_at is not None
                and document.processing_started_at > stale_before
            ):
                return None, MedicalDocument.ProcessingStatus.PROCESSING
        document.processing_status = MedicalDocument.ProcessingStatus.PROCESSING
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
        _event(document, MedicalDocumentEvent.EventType.PDF_EXTRACTION_STARTED)
        return document, ""


def _persist_result(document_uuid, result, *, reprocess=False):
    try:
        with transaction.atomic():
            document = (
                MedicalDocument.objects.select_for_update()
                .select_related("stored_file")
                .filter(uuid=document_uuid)
                .first()
            )
            if document is None or (
                document.archive_status != MedicalDocument.ArchiveStatus.ACTIVE
            ):
                return "SKIPPED"
            has_canonical = hasattr(document, "document_text")
            if has_canonical and not reprocess:
                return _canonical_outcome(document.document_text)
            if not _valid_result(result, document.stored_file):
                return _mark_failure(document_uuid, "pdf_malformed_result")
            if has_canonical:
                document.document_text.delete()

            metadata = result.metadata
            extracted = DocumentText.objects.create(
                document=document,
                text=result.text,
                page_count=result.page_count,
                character_count=result.character_count,
                meaningful_character_count=metadata["meaningful_character_count"],
                usable=result.usable,
                usability_reason=result.reason,
                has_pages_requiring_ocr=any(page.requires_ocr for page in result.pages),
                extraction_method=metadata["extraction_method"],
                extractor_name=metadata["extractor_name"],
                extractor_version=metadata["extractor_version"],
                pipeline_version=metadata["pipeline_version"],
            )
            DocumentTextPage.objects.bulk_create(
                [
                    DocumentTextPage(
                        document_text=extracted,
                        page_number=page.page_number,
                        text=page.text,
                        native_text=page.text,
                        meaningful_character_count=(page.meaningful_character_count),
                        requires_ocr=page.requires_ocr,
                        effective_source=DocumentTextPage.EffectiveSource.PDF_TEXT,
                    )
                    for page in result.pages
                ]
            )
            outcome = _canonical_outcome(extracted)
            document.processing_status = outcome
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
            event_type = (
                MedicalDocumentEvent.EventType.PDF_TEXT_EXTRACTED
                if result.usable
                else MedicalDocumentEvent.EventType.PDF_OCR_REQUIRED
            )
            _event(
                document,
                event_type,
                {
                    "page_count": result.page_count,
                    "pages_requiring_ocr": len(metadata["pages_requiring_ocr"]),
                },
            )
            if extracted.has_pages_requiring_ocr:
                from processing.ocr_services import schedule_ocr

                schedule_ocr(document)
            elif outcome == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED:
                from processing.date_services import schedule_date_processing

                schedule_date_processing(document)
            return outcome
    except DatabaseError:
        return _mark_failure(document_uuid, "pdf_persistence_failed")


def process_pdf_document(document_uuid, *, extractor=None, reprocess=False):
    started_at = time.monotonic()
    document, outcome = _claim(document_uuid, reprocess=reprocess)
    if document is None:
        return outcome

    try:
        content, failure_code = _read_verified_content(document.stored_file)
        if failure_code:
            return _mark_failure(document_uuid, failure_code)
        result = (extractor or PDFTextExtractor()).extract(content)
    except RetryablePDFProcessingError as exc:
        _mark_failure(document_uuid, exc.code, retryable=True)
        raise
    except PDFExtractionError as exc:
        return _mark_failure(document_uuid, exc.code)
    except Exception:
        return _mark_failure(document_uuid, "pdf_extraction_failed")

    if not _valid_result(result, document.stored_file):
        return _mark_failure(document_uuid, "pdf_malformed_result")
    outcome = _persist_result(document_uuid, result, reprocess=reprocess)
    if outcome in {
        MedicalDocument.ProcessingStatus.TEXT_EXTRACTED,
        MedicalDocument.ProcessingStatus.OCR_REQUIRED,
    }:
        logger.info(
            "PDF extraction completed",
            extra={
                "document_uuid": str(document_uuid),
                "processing_status": str(outcome),
                "page_count": result.page_count,
                "duration_ms": int((time.monotonic() - started_at) * 1000),
            },
        )
    return outcome
