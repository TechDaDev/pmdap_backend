import logging
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone

from documents.models import (
    MedicalDocument,
    MedicalDocumentEvent,
    MedicalDocumentPage,
)
from processing.dates import choose_suggested_index, detect_page_dates
from processing.models import DateCandidate

logger = logging.getLogger(__name__)


class DateProcessingError(Exception):
    retryable = False

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class RetryableDateProcessingError(DateProcessingError):
    retryable = True


@dataclass(frozen=True)
class DateTextPageSnapshot:
    page_number: int
    text: str
    source: str


@dataclass(frozen=True)
class DateProcessingClaim:
    document_uuid: str
    source_text_uuid: str
    started_at: object
    pages: tuple[DateTextPageSnapshot, ...]


def _event(document, event_type, metadata=None):
    MedicalDocumentEvent.objects.create(
        document=document,
        event_type=event_type,
        actor=None,
        metadata=metadata or {},
    )


def schedule_date_processing(document):
    _event(document, MedicalDocumentEvent.EventType.DATE_PROCESSING_QUEUED)
    transaction.on_commit(lambda: _enqueue_date_processing(str(document.uuid)))


def _enqueue_date_processing(document_uuid):
    from processing.tasks import detect_document_dates

    try:
        detect_document_dates.delay(document_uuid)
    except Exception:
        logger.error(
            "Date processing enqueue failed",
            extra={"document_uuid": document_uuid},
        )


def _configuration_valid():
    return (
        1 <= settings.DATE_CONTEXT_MAX_CHARS <= 256
        and 0.0 <= settings.DATE_SUGGESTION_MIN_SCORE <= 1.0
        and 0.0 <= settings.DATE_SUGGESTION_TIE_TOLERANCE <= 1.0
        and settings.DATE_FUTURE_TOLERANCE_DAYS >= 0
        and settings.DATE_MAX_CANDIDATES_PER_DOCUMENT >= 1
        and 0 < len(settings.DATE_PIPELINE_VERSION) <= 32
    )


def _claim(document_uuid, *, reprocess=False):
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
            return None, "SKIPPED"
        if not reprocess and document.processing_status in {
            MedicalDocument.ProcessingStatus.DATE_DETECTED,
            MedicalDocument.ProcessingStatus.DATE_NOT_FOUND,
            MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION,
            MedicalDocument.ProcessingStatus.DATE_CONFIRMED,
        }:
            return None, document.processing_status
        if (
            document.processing_status
            == MedicalDocument.ProcessingStatus.DATE_PROCESSING
        ):
            stale_before = timezone.now() - timedelta(
                seconds=settings.DATE_TASK_TIME_LIMIT
            )
            if (
                document.processing_started_at is not None
                and document.processing_started_at > stale_before
            ):
                return None, MedicalDocument.ProcessingStatus.DATE_PROCESSING
        elif (
            document.processing_status
            != MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
            and not (
                reprocess
                and document.processing_status
                in {
                    MedicalDocument.ProcessingStatus.DATE_DETECTED,
                    MedicalDocument.ProcessingStatus.DATE_NOT_FOUND,
                    MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION,
                    MedicalDocument.ProcessingStatus.DATE_CONFIRMED,
                    MedicalDocument.ProcessingStatus.FAILED,
                }
            )
        ):
            return None, document.processing_status
        if not hasattr(document, "document_text"):
            return None, _set_failure(document, "date_canonical_text_missing")
        started_at = timezone.now()
        document.processing_status = MedicalDocument.ProcessingStatus.DATE_PROCESSING
        document.processing_failure_code = ""
        document.processing_started_at = started_at
        document.save(
            update_fields=(
                "processing_status",
                "processing_failure_code",
                "processing_started_at",
                "updated_at",
            )
        )
        pages = tuple(
            DateTextPageSnapshot(page.page_number, page.text, page.effective_source)
            for page in document.document_text.pages.order_by("page_number")
        )
        if not pages:
            pages = (
                DateTextPageSnapshot(
                    1,
                    document.document_text.text,
                    (
                        DateCandidate.Source.OCR
                        if document.document_text.extraction_method == "OCR"
                        else DateCandidate.Source.PDF_TEXT
                    ),
                ),
            )
        _event(
            document,
            MedicalDocumentEvent.EventType.DATE_PROCESSING_STARTED,
            {"pipeline_version": settings.DATE_PIPELINE_VERSION},
        )
        return (
            DateProcessingClaim(
                str(document.uuid),
                str(document.document_text.uuid),
                started_at,
                pages,
            ),
            "",
        )


def _set_failure(document, code, *, retryable=False):
    document.processing_status = (
        MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
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
        MedicalDocumentEvent.EventType.DATE_PROCESSING_FAILED,
        {"failure_code": code, "retryable": retryable},
    )
    return document.processing_status


def _mark_failure(claim, code, *, retryable=False):
    with transaction.atomic():
        document = (
            MedicalDocument.objects.select_for_update()
            .filter(uuid=claim.document_uuid)
            .first()
        )
        if document is None or (
            document.archive_status != MedicalDocument.ArchiveStatus.ACTIVE
        ):
            return "SKIPPED"
        if (
            document.processing_status
            != MedicalDocument.ProcessingStatus.DATE_PROCESSING
            or document.processing_started_at != claim.started_at
        ):
            return document.processing_status
        outcome = _set_failure(document, code, retryable=retryable)
    logger.warning(
        "Date processing failed",
        extra={
            "document_uuid": claim.document_uuid,
            "processing_status": outcome,
            "failure_code": code,
        },
    )
    return outcome


def _persist(claim, candidates):
    with transaction.atomic():
        document = (
            MedicalDocument.objects.select_for_update()
            .filter(uuid=claim.document_uuid)
            .first()
        )
        if (
            document is None
            or document.archive_status != MedicalDocument.ArchiveStatus.ACTIVE
        ):
            return "SKIPPED"
        if (
            document.processing_status
            != MedicalDocument.ProcessingStatus.DATE_PROCESSING
            or document.processing_started_at != claim.started_at
        ):
            return document.processing_status
        if (
            not hasattr(document, "document_text")
            or str(document.document_text.uuid) != claim.source_text_uuid
        ):
            return _set_failure(document, "date_source_text_changed", retryable=True)

        suggested_index = choose_suggested_index(
            candidates,
            minimum_score=settings.DATE_SUGGESTION_MIN_SCORE,
            tie_tolerance=settings.DATE_SUGGESTION_TIE_TOLERANCE,
        )
        candidate_set_uuid = uuid.uuid4()
        DateCandidate.objects.filter(document=document, is_current=True).update(
            is_current=False
        )
        DateCandidate.objects.bulk_create(
            [
                DateCandidate(
                    document=document,
                    detected_date=candidate.detected_date,
                    alternative_date=candidate.alternative_date,
                    raw_value=candidate.raw_value,
                    normalized_value=candidate.normalized_value,
                    candidate_type=candidate.candidate_type.value,
                    score=candidate.score,
                    page_number=candidate.page_number,
                    context=candidate.context,
                    source=candidate.source,
                    occurrence_index=candidate.occurrence_index,
                    ambiguous=candidate.ambiguous,
                    parsing_rule=candidate.parsing_rule,
                    pipeline_version=settings.DATE_PIPELINE_VERSION,
                    is_suggested=index == suggested_index,
                    candidate_set_uuid=candidate_set_uuid,
                    is_current=True,
                )
                for index, candidate in enumerate(candidates)
            ]
        )
        document.processing_status = (
            MedicalDocument.ProcessingStatus.DATE_CONFIRMED
            if document.date_verified
            else MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION
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
        from documents.page_services import sync_document_to_page_units

        sync_document_to_page_units(document)
        _event(
            document,
            (
                MedicalDocumentEvent.EventType.DATE_CANDIDATES_DETECTED
                if candidates
                else MedicalDocumentEvent.EventType.DATE_NOT_FOUND
            ),
            {
                "candidate_count": len(candidates),
                "suggestion_present": suggested_index is not None,
                "pipeline_version": settings.DATE_PIPELINE_VERSION,
                "candidate_set_uuid": str(candidate_set_uuid),
            },
        )
        return document.processing_status


def process_date_candidates(
    document_uuid, *, detector=detect_page_dates, reprocess=False
):
    started = time.monotonic()
    try:
        claim, outcome = _claim(document_uuid, reprocess=reprocess)
    except DatabaseError as exc:
        raise RetryableDateProcessingError("date_database_retryable") from exc
    if claim is None:
        return outcome
    try:
        if not _configuration_valid():
            raise DateProcessingError("date_configuration_invalid")
        candidates = []
        for page in claim.pages:
            candidates.extend(
                detector(
                    page.text,
                    page_number=page.page_number,
                    source=page.source,
                    context_max_chars=settings.DATE_CONTEXT_MAX_CHARS,
                    future_tolerance_days=settings.DATE_FUTURE_TOLERANCE_DAYS,
                )
            )
            if len(candidates) > settings.DATE_MAX_CANDIDATES_PER_DOCUMENT:
                raise DateProcessingError("date_candidate_limit_exceeded")
        outcome = _persist(claim, tuple(candidates))
    except RetryableDateProcessingError as exc:
        _mark_failure(claim, exc.code, retryable=True)
        raise
    except DatabaseError as exc:
        _mark_failure(claim, "date_database_retryable", retryable=True)
        raise RetryableDateProcessingError("date_database_retryable") from exc
    except DateProcessingError as exc:
        return _mark_failure(claim, exc.code)
    except Exception:
        return _mark_failure(claim, "date_processing_failed")
    logger.info(
        "Date processing completed",
        extra={
            "document_uuid": str(document_uuid),
            "candidate_count": len(candidates),
            "suggestion_present": any(
                candidate.is_suggested
                for candidate in DateCandidate.objects.filter(
                    document_id=document_uuid, is_current=True
                )
            ),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "pipeline_version": settings.DATE_PIPELINE_VERSION,
            "processing_status": outcome,
        },
    )
    return outcome


# --------------------------------------------------------------------------- #
# Page-scoped date processing (multi-page PDFs)
# --------------------------------------------------------------------------- #


def schedule_page_date_processing(page_unit):
    _event(
        page_unit.document,
        MedicalDocumentEvent.EventType.DATE_PROCESSING_QUEUED,
        {"page_number": page_unit.page_number},
    )
    transaction.on_commit(
        lambda: _enqueue_page_date(str(page_unit.uuid))
    )


def _enqueue_page_date(page_unit_uuid):
    from processing.tasks import detect_page_dates

    try:
        detect_page_dates.delay(page_unit_uuid)
    except Exception:
        logger.error(
            "Page date processing enqueue failed",
            extra={"page_unit_uuid": page_unit_uuid},
        )


def process_page_date_candidates(page_unit_uuid, *, detector=detect_page_dates):
    """Detect date candidates for ONE report page unit (page text only).

    Page text is the ONLY input — no cross-page date leakage. Candidates are
    scoped to the page unit; the page then advances toward AWAITING_CONFIRMATION
    once its lab extraction is also terminal.
    """
    from documents.page_services import _finalize_page

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
            source_page = (
                document.document_text.pages.filter(
                    page_number=page.page_number
                ).first()
                if hasattr(document, "document_text")
                else None
            )
            if source_page is None:
                _finalize_page(page)
                return page.processing_status
            text = source_page.text
            source = (
                DateCandidate.Source.OCR
                if source_page.effective_source == "OCR"
                else DateCandidate.Source.PDF_TEXT
            )
    except DatabaseError as exc:
        raise RetryableDateProcessingError("date_database_retryable") from exc

    if not _configuration_valid():
        return _mark_page_failure(page, "date_configuration_invalid")
    try:
        candidates = detector(
            text,
            page_number=page.page_number,
            source=source,
            context_max_chars=settings.DATE_CONTEXT_MAX_CHARS,
            future_tolerance_days=settings.DATE_FUTURE_TOLERANCE_DAYS,
        )
        if len(candidates) > settings.DATE_MAX_CANDIDATES_PER_DOCUMENT:
            raise DateProcessingError("date_candidate_limit_exceeded")
    except DateProcessingError as exc:
        return _mark_page_failure(page, exc.code)
    except Exception:
        return _mark_page_failure(page, "date_processing_failed")

    _persist_page_candidates(page, candidates, started_at=started)
    return page.processing_status


def _mark_page_failure(page, code):
    from documents.page_services import recalculate_document_processing_state

    page.processing_status = MedicalDocumentPage.ProcessingStatus.FAILED
    page.processing_failure_code = code
    page.save(
        update_fields=("processing_status", "processing_failure_code", "updated_at")
    )
    recalculate_document_processing_state(page.document)
    return page.processing_status


def _persist_page_candidates(page, candidates, *, started_at=None):
    from documents.page_services import recalculate_document_processing_state

    with transaction.atomic():
        document = page.document
        suggested_index = choose_suggested_index(
            candidates,
            minimum_score=settings.DATE_SUGGESTION_MIN_SCORE,
            tie_tolerance=settings.DATE_SUGGESTION_TIE_TOLERANCE,
        )
        candidate_set_uuid = uuid.uuid4()
        DateCandidate.objects.filter(
            document=document, page_number=page.page_number, is_current=True
        ).update(is_current=False)
        DateCandidate.objects.bulk_create(
            [
                DateCandidate(
                    document=document,
                    page_unit=page,
                    detected_date=candidate.detected_date,
                    alternative_date=candidate.alternative_date,
                    raw_value=candidate.raw_value,
                    normalized_value=candidate.normalized_value,
                    candidate_type=candidate.candidate_type.value,
                    score=candidate.score,
                    page_number=candidate.page_number,
                    context=candidate.context,
                    source=candidate.source,
                    occurrence_index=candidate.occurrence_index,
                    ambiguous=candidate.ambiguous,
                    parsing_rule=candidate.parsing_rule,
                    pipeline_version=settings.DATE_PIPELINE_VERSION,
                    is_suggested=index == suggested_index,
                    candidate_set_uuid=candidate_set_uuid,
                    is_current=True,
                )
                for index, candidate in enumerate(candidates)
            ]
        )
        _event(
            document,
            (
                MedicalDocumentEvent.EventType.DATE_CANDIDATES_DETECTED
                if candidates
                else MedicalDocumentEvent.EventType.DATE_NOT_FOUND
            ),
            {
                "page_number": page.page_number,
                "candidate_count": len(candidates),
                "suggestion_present": suggested_index is not None,
                "pipeline_version": settings.DATE_PIPELINE_VERSION,
                "candidate_set_uuid": str(candidate_set_uuid),
            },
        )
    if started_at is not None:
        logger.info(
            "Page date processing completed",
            extra={
                "document_uuid": str(document.uuid),
                "page_number": page.page_number,
                "candidate_count": len(candidates),
                "duration_ms": int((time.monotonic() - started_at) * 1000),
                "pipeline_version": settings.DATE_PIPELINE_VERSION,
            },
        )
    # Advance page once lab extraction is also terminal.
    from documents.page_services import _finalize_page

    _finalize_page(page)
    return page.processing_status
