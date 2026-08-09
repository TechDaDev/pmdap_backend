from unittest.mock import patch

import pytest
from celery.exceptions import MaxRetriesExceededError, Retry
from django.conf import settings
from django.test import override_settings

from processing.ocr_services import RetryableOCRProcessingError
from processing.tasks import ocr_medical_document


@override_settings(OCR_TASK_MAX_RETRIES=3, OCR_TASK_RETRY_BASE_SECONDS=10)
def test_ocr_task_retries_only_retryable_failures_with_bounded_backoff():
    failure = RetryableOCRProcessingError("ocr_resource_retryable")
    with (
        patch("processing.ocr_services.process_ocr_document", side_effect=failure),
        patch.object(ocr_medical_document, "retry", side_effect=Retry()) as retry,
        pytest.raises(Retry),
    ):
        ocr_medical_document.run("00000000-0000-0000-0000-000000000001")

    retry.assert_called_once_with(exc=failure, countdown=10, max_retries=3)


def test_ocr_task_returns_terminal_outcome_without_retry():
    with (
        patch("processing.ocr_services.process_ocr_document", return_value="FAILED"),
        patch.object(ocr_medical_document, "retry") as retry,
    ):
        outcome = ocr_medical_document.run("00000000-0000-0000-0000-000000000001")

    assert outcome == "FAILED"
    retry.assert_not_called()


def test_ocr_task_has_dedicated_queue_and_timeout_policy():
    assert ocr_medical_document.queue == "ocr"
    assert ocr_medical_document.soft_time_limit == settings.OCR_TASK_SOFT_TIME_LIMIT
    assert ocr_medical_document.time_limit == settings.OCR_TASK_TIME_LIMIT
    assert ocr_medical_document.soft_time_limit < ocr_medical_document.time_limit


def test_ocr_task_retry_exhaustion_is_terminal():
    failure = RetryableOCRProcessingError("ocr_resource_retryable")
    exhausted = MaxRetriesExceededError("bounded retry limit reached")
    with (
        patch("processing.ocr_services.process_ocr_document", side_effect=failure),
        patch.object(ocr_medical_document, "retry", side_effect=exhausted) as retry,
        pytest.raises(MaxRetriesExceededError),
    ):
        ocr_medical_document.run("00000000-0000-0000-0000-000000000001")

    retry.assert_called_once_with(
        exc=failure,
        countdown=settings.OCR_TASK_RETRY_BASE_SECONDS,
        max_retries=settings.OCR_TASK_MAX_RETRIES,
    )
