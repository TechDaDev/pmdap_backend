import threading

import pytest
from django.db import close_old_connections, connection

from documents.date_services import confirm_document_date
from documents.exceptions import (
    DateCandidateStale,
    InvalidDateConfirmationState,
    MedicalDocumentNotFound,
)
from documents.models import DocumentDateEvent, MedicalDocument
from documents.services import soft_delete_medical_document
from processing.date_services import process_date_candidates
from tests.test_document_date_confirmation import ready_document

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only M10 concurrency tests")


def run_concurrently(*operations):
    barrier = threading.Barrier(len(operations))
    results = []
    failures = []

    def run(operation):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            results.append(operation())
        except Exception as exc:
            failures.append(exc)
        finally:
            close_old_connections()

    threads = [
        threading.Thread(target=run, args=(operation,)) for operation in operations
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    return results, failures


def decision(document_uuid, actor_uuid, *, candidate_uuid=None, manual_date=None):
    def operation():
        from accounts.models import User

        document = MedicalDocument.objects.get(uuid=document_uuid)
        actor = User.objects.get(uuid=actor_uuid)
        result = confirm_document_date(
            document=document,
            actor=actor,
            candidate_id=candidate_uuid,
            manual_date=manual_date,
        )
        return result.document_date

    return operation


def test_candidate_confirmation_vs_manual_correction_is_atomic():
    require_postgresql()
    document = ready_document()
    candidate = document.date_candidates.get(is_current=True, is_suggested=True)

    results, failures = run_concurrently(
        decision(
            document.uuid,
            document.patient.user_id,
            candidate_uuid=candidate.uuid,
        ),
        decision(
            document.uuid,
            document.patient.user_id,
            manual_date=candidate.detected_date.replace(day=10),
        ),
    )

    document.refresh_from_db()
    events = list(document.date_events.all())
    assert not failures
    assert len(results) == 2
    assert len(events) == 2
    assert document.document_date == events[-1].new_date
    assert document.date_source == events[-1].source


def test_two_candidate_confirmations_serialize_to_last_event():
    require_postgresql()
    document = ready_document()
    candidates = list(document.date_candidates.filter(is_current=True))

    results, failures = run_concurrently(
        *[
            decision(
                document.uuid,
                document.patient.user_id,
                candidate_uuid=candidate.uuid,
            )
            for candidate in candidates
        ]
    )

    document.refresh_from_db()
    events = list(document.date_events.all())
    assert not failures
    assert len(results) == len(candidates) == 2
    assert len(events) == 2
    assert document.document_date == events[-1].new_date


def test_same_confirmation_concurrent_retry_creates_one_event():
    require_postgresql()
    document = ready_document()
    candidate = document.date_candidates.get(is_current=True, is_suggested=True)
    operation = decision(
        document.uuid,
        document.patient.user_id,
        candidate_uuid=candidate.uuid,
    )

    results, failures = run_concurrently(operation, operation)

    assert not failures
    assert len(results) == 2
    assert DocumentDateEvent.objects.filter(document=document).count() == 1


def test_reprocessing_vs_confirmation_never_accepts_replaced_candidate():
    require_postgresql()
    document = ready_document()
    old_candidate = document.date_candidates.get(is_current=True, is_suggested=True)

    results, failures = run_concurrently(
        lambda: process_date_candidates(str(document.uuid), reprocess=True),
        decision(
            document.uuid,
            document.patient.user_id,
            candidate_uuid=old_candidate.uuid,
        ),
    )

    document.refresh_from_db()
    old_candidate.refresh_from_db()
    assert len(results) + len(failures) == 2
    assert all(
        isinstance(exc, (DateCandidateStale, InvalidDateConfirmationState))
        for exc in failures
    )
    if old_candidate.is_current is False:
        assert not document.date_events.filter(candidate=old_candidate).exists()
    assert document.processing_status in {
        MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION,
        MedicalDocument.ProcessingStatus.DATE_CONFIRMED,
    }


def test_soft_delete_vs_confirmation_keeps_document_and_event_consistent():
    require_postgresql()
    document = ready_document()

    def delete():
        from accounts.models import User

        locked_document = MedicalDocument.objects.get(uuid=document.uuid)
        actor = User.objects.get(uuid=document.patient.user_id)
        return soft_delete_medical_document(document=locked_document, actor=actor)

    results, failures = run_concurrently(
        delete,
        decision(
            document.uuid,
            document.patient.user_id,
            manual_date=document.created_at.date(),
        ),
    )

    document.refresh_from_db()
    assert len(results) + len(failures) == 2
    assert all(isinstance(exc, MedicalDocumentNotFound) for exc in failures)
    assert document.archive_status == MedicalDocument.ArchiveStatus.DELETED
    if document.date_events.exists():
        assert document.date_verified is True
        assert (
            document.document_date == document.date_events.latest("created_at").new_date
        )
