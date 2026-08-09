import threading

import pytest
from django.db import close_old_connections, connection

from documents.models import MedicalDocument
from processing.date_services import process_date_candidates
from processing.dates import detect_page_dates
from processing.models import DateCandidate
from tests.test_date_processing import prepared_document

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only date-processing concurrency test")


class BlockingDetector:
    def __init__(self, *, failure=None):
        self.failure = failure
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args, **kwargs):
        self.started.set()
        assert self.release.wait(timeout=10)
        if self.failure is not None:
            raise self.failure
        return detect_page_dates(*args, **kwargs)


def run_worker(document_uuid, detector):
    results = []
    failures = []

    def operation():
        close_old_connections()
        try:
            results.append(process_date_candidates(document_uuid, detector=detector))
        except Exception as exc:
            failures.append((exc, exc.__cause__))
        finally:
            close_old_connections()

    thread = threading.Thread(target=operation)
    thread.start()
    return thread, results, failures


def finish(thread, detector):
    detector.release.set()
    thread.join(timeout=20)
    assert not thread.is_alive()


def test_two_workers_leave_one_canonical_candidate_set_and_suggestion():
    require_postgresql()
    document = prepared_document("DOB: 21/06/1985\nReport Date: 14/03/2026")
    detector = BlockingDetector()
    thread, results, failures = run_worker(str(document.uuid), detector)
    assert detector.started.wait(timeout=10), failures

    second = process_date_candidates(str(document.uuid))
    finish(thread, detector)

    document.refresh_from_db()
    assert not failures
    assert results == [MedicalDocument.ProcessingStatus.DATE_DETECTED]
    assert second == MedicalDocument.ProcessingStatus.DATE_PROCESSING
    assert document.date_candidates.count() == 2
    assert document.date_candidates.filter(is_suggested=True).count() == 1


def test_stale_worker_failure_cannot_overwrite_successful_result():
    require_postgresql()
    document = prepared_document("Report Date: 14/03/2026")
    detector = BlockingDetector(failure=RuntimeError("stale private failure"))
    thread, results, failures = run_worker(str(document.uuid), detector)
    assert detector.started.wait(timeout=10), failures

    MedicalDocument.objects.filter(pk=document.pk).update(
        processing_status=MedicalDocument.ProcessingStatus.DATE_DETECTED,
        processing_started_at=None,
        processing_failure_code="",
    )
    finish(thread, detector)

    document.refresh_from_db()
    assert not failures
    assert results == [MedicalDocument.ProcessingStatus.DATE_DETECTED]
    assert document.processing_status == MedicalDocument.ProcessingStatus.DATE_DETECTED
    assert document.processing_failure_code == ""


def test_worker_finishing_after_soft_delete_does_not_resurrect_document():
    require_postgresql()
    document = prepared_document("Report Date: 14/03/2026")
    detector = BlockingDetector()
    thread, results, failures = run_worker(str(document.uuid), detector)
    assert detector.started.wait(timeout=10), failures

    MedicalDocument.objects.filter(pk=document.pk).update(
        archive_status=MedicalDocument.ArchiveStatus.DELETED
    )
    finish(thread, detector)

    document.refresh_from_db()
    assert not failures
    assert results == ["SKIPPED"]
    assert document.archive_status == MedicalDocument.ArchiveStatus.DELETED
    assert not DateCandidate.objects.filter(document=document).exists()
