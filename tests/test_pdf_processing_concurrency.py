import threading
from unittest.mock import Mock

import pytest
from django.db import close_old_connections, connection
from django.test import override_settings

from documents.models import MedicalDocument
from processing.models import DocumentText, DocumentTextPage
from processing.services import process_pdf_document
from tests.test_pdf_processing import extracted_result, queued_document

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only PDF processing concurrency test")


class BlockingExtractor:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def extract(self, content):
        del content
        self.started.set()
        assert self.release.wait(timeout=10)
        return extracted_result()


def test_concurrent_delivery_creates_one_canonical_result_and_pages(tmp_path):
    require_postgresql()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        document = queued_document(tmp_path)
        extractor = BlockingExtractor()
        results = []
        failures = []

        def first_worker():
            close_old_connections()
            try:
                results.append(
                    process_pdf_document(str(document.uuid), extractor=extractor)
                )
            except Exception as exc:  # exact empty outcome asserted below
                failures.append(exc)
            finally:
                close_old_connections()

        worker = threading.Thread(target=first_worker)
        worker.start()
        assert extractor.started.wait(timeout=10)
        second = process_pdf_document(str(document.uuid), extractor=Mock())
        extractor.release.set()
        worker.join(timeout=20)
        assert not worker.is_alive()

    document.refresh_from_db()
    assert not failures
    assert results == ["TEXT_EXTRACTED"]
    assert second == "PROCESSING"
    assert document.processing_status == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
    assert DocumentText.objects.filter(document=document).count() == 1
    assert (
        DocumentTextPage.objects.filter(document_text__document=document).count() == 2
    )

    late_failure = Mock()
    late_failure.extract.side_effect = RuntimeError("late worker failure")
    assert (
        process_pdf_document(str(document.uuid), extractor=late_failure)
        == "TEXT_EXTRACTED"
    )
    late_failure.extract.assert_not_called()
    document.refresh_from_db()
    assert document.processing_failure_code == ""
