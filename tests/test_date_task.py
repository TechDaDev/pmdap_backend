from unittest.mock import Mock, patch

from processing.date_services import RetryableDateProcessingError
from processing.tasks import detect_document_dates


def test_date_task_delegates_to_service():
    task = detect_document_dates
    with patch(
        "processing.date_services.process_date_candidates",
        return_value="DATE_DETECTED",
    ) as process:
        assert task.run("document-uuid") == "DATE_DETECTED"

    process.assert_called_once_with("document-uuid")


def test_date_task_uses_bounded_exponential_retry(settings):
    settings.DATE_TASK_RETRY_BASE_SECONDS = 7
    settings.DATE_TASK_MAX_RETRIES = 3
    task = detect_document_dates
    task.request_stack.push(Mock(retries=2, called_directly=False))
    try:
        with (
            patch(
                "processing.date_services.process_date_candidates",
                side_effect=RetryableDateProcessingError("date_database_retryable"),
            ),
            patch.object(task, "retry", side_effect=RuntimeError("retry")) as retry,
        ):
            try:
                task.run("document-uuid")
            except RuntimeError as exc:
                assert str(exc) == "retry"
    finally:
        task.request_stack.pop()

    assert retry.call_args.kwargs["countdown"] == 28
    assert retry.call_args.kwargs["max_retries"] == 3
