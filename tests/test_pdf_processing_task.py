from unittest.mock import patch

import pytest
from celery.exceptions import Retry
from django.conf import settings
from django.test import override_settings

from processing.services import RetryablePDFProcessingError
from processing.tasks import extract_pdf_text


@override_settings(
    PDF_EXTRACTION_MAX_RETRIES=3,
    PDF_EXTRACTION_RETRY_BASE_SECONDS=5,
)
def test_task_retries_only_retryable_storage_failure_with_bounded_backoff():
    failure = RetryablePDFProcessingError("medical_file_read_retryable")
    with (
        patch(
            "processing.services.process_pdf_document",
            side_effect=failure,
        ),
        patch.object(extract_pdf_text, "retry", side_effect=Retry()) as retry,
        pytest.raises(Retry),
    ):
        extract_pdf_text.run("00000000-0000-0000-0000-000000000001")

    retry.assert_called_once_with(exc=failure, countdown=5, max_retries=3)


def test_task_returns_terminal_outcome_without_retry():
    with (
        patch(
            "processing.services.process_pdf_document",
            return_value="FAILED",
        ),
        patch.object(extract_pdf_text, "retry") as retry,
    ):
        outcome = extract_pdf_text.run("00000000-0000-0000-0000-000000000001")

    assert outcome == "FAILED"
    retry.assert_not_called()


def test_pdf_task_has_explicit_soft_and_hard_timeout_policy():
    assert extract_pdf_text.soft_time_limit == settings.PDF_EXTRACTION_SOFT_TIME_LIMIT
    assert extract_pdf_text.time_limit == settings.PDF_EXTRACTION_TIME_LIMIT
    assert extract_pdf_text.soft_time_limit < extract_pdf_text.time_limit
