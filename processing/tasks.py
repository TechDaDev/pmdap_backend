from celery import shared_task
from django.conf import settings


@shared_task(
    bind=True,
    name="processing.extract_pdf_text",
    soft_time_limit=settings.PDF_EXTRACTION_SOFT_TIME_LIMIT,
    time_limit=settings.PDF_EXTRACTION_TIME_LIMIT,
)
def extract_pdf_text(self, document_uuid):
    from processing.services import (
        RetryablePDFProcessingError,
        process_pdf_document,
    )

    try:
        return process_pdf_document(document_uuid)
    except RetryablePDFProcessingError as exc:
        countdown = settings.PDF_EXTRACTION_RETRY_BASE_SECONDS * (
            2**self.request.retries
        )
        raise self.retry(
            exc=exc,
            countdown=countdown,
            max_retries=settings.PDF_EXTRACTION_MAX_RETRIES,
        ) from exc


@shared_task(
    bind=True,
    name="processing.ocr_medical_document",
    queue="ocr",
    soft_time_limit=settings.OCR_TASK_SOFT_TIME_LIMIT,
    time_limit=settings.OCR_TASK_TIME_LIMIT,
)
def ocr_medical_document(self, document_uuid):
    from processing.ocr_services import (
        RetryableOCRProcessingError,
        process_ocr_document,
    )

    try:
        return process_ocr_document(document_uuid)
    except RetryableOCRProcessingError as exc:
        countdown = settings.OCR_TASK_RETRY_BASE_SECONDS * (2**self.request.retries)
        raise self.retry(
            exc=exc,
            countdown=countdown,
            max_retries=settings.OCR_TASK_MAX_RETRIES,
        ) from exc


@shared_task(
    bind=True,
    name="processing.detect_document_dates",
    soft_time_limit=settings.DATE_TASK_SOFT_TIME_LIMIT,
    time_limit=settings.DATE_TASK_TIME_LIMIT,
)
def detect_document_dates(self, document_uuid):
    from processing.date_services import (
        RetryableDateProcessingError,
        process_date_candidates,
    )

    try:
        return process_date_candidates(document_uuid)
    except RetryableDateProcessingError as exc:
        countdown = settings.DATE_TASK_RETRY_BASE_SECONDS * (2**self.request.retries)
        raise self.retry(
            exc=exc,
            countdown=countdown,
            max_retries=settings.DATE_TASK_MAX_RETRIES,
        ) from exc
