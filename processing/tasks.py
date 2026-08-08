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
