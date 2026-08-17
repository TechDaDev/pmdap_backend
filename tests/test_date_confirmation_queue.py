"""Document-centric date-confirmation queue.

The queue, its count, the Home badge, and the archive UNCONFIRMED filter all
derive from ONE domain rule: active + AWAITING_CONFIRMATION + not
user-confirmed. Documents with ZERO OCR candidates still appear (manual date
fallback). A confirmed document leaves the queue but stays in the archive.
"""
from datetime import date

import pytest

from documents.models import MedicalDocument
from processing.models import DateCandidate
from tests.archive_helpers import make_document, verified_document
from tests.test_medical_documents_api import patient_user

pytestmark = pytest.mark.django_db

QUEUE = "/api/v1/documents/date-confirmations/pending/"
ARCHIVE = "/api/v1/archive/"
SUMMARY = "/api/v1/archive/summary/"


def authenticate(client, user):
    client.force_authenticate(user=user)


def awaiting(patient, user, **kwargs):
    return make_document(
        patient,
        user,
        processing_status="AWAITING_CONFIRMATION",
        **kwargs,
    )


def make_candidate(document, detected_date, *, score=0.9, suggested=True):
    return DateCandidate.objects.create(
        document=document,
        detected_date=detected_date,
        raw_value="14/03/2026",
        normalized_value="14/03/2026",
        candidate_type=DateCandidate.CandidateType.REPORT_DATE,
        score=score,
        page_number=1,
        context="",
        source=DateCandidate.Source.OCR,
        occurrence_index=1,
        parsing_rule="DMY_NUMERIC",
        pipeline_version="m9-date-v2",
        is_suggested=suggested,
        is_current=True,
    )


def confirm_endpoint(document):
    return f"/api/v1/documents/{document.uuid}/confirm-date/"


def test_pending_document_with_zero_candidates_appears_in_queue(api_client):
    user, patient = patient_user()
    document = awaiting(patient, user)
    authenticate(api_client, user)

    response = api_client.get(QUEUE)

    assert response.status_code == 200
    body = response.data["data"]
    assert body["count"] == 1
    item = body["results"][0]
    assert item["document_uuid"] == document.uuid
    assert item["processing_status"] == "AWAITING_CONFIRMATION"
    assert item["detected_candidates"] == []
    assert item["requires_manual_date"] is True


def test_manual_date_with_zero_candidate_document(api_client):
    user, patient = patient_user()
    document = awaiting(patient, user)
    authenticate(api_client, user)

    response = api_client.post(
        confirm_endpoint(document),
        {"date": "2026-03-14"},
        format="json",
    )

    assert response.status_code == 200
    data = response.data["data"]
    assert data["document_date"] == "2026-03-14"
    assert data["date_verified"] is True
    assert data["date_source"] == "USER_CORRECTED"
    assert data["processing_status"] == "DATE_CONFIRMED"
    document.refresh_from_db()
    assert not document_needs_confirmation(document)


def document_needs_confirmation(document):
    return (
        document.processing_status
        == MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION
        and not document.date_verified
    )


def test_pending_document_with_one_candidate(api_client):
    user, patient = patient_user()
    document = awaiting(patient, user)
    make_candidate(document, date(2026, 3, 14), suggested=True)
    authenticate(api_client, user)

    response = api_client.get(QUEUE)
    item = response.data["data"]["results"][0]
    assert item["requires_manual_date"] is False
    assert len(item["detected_candidates"]) == 1
    cand = item["detected_candidates"][0]
    assert cand["date"] == date(2026, 3, 14)
    assert cand["confidence"] == 0.9


def test_pending_document_with_multiple_candidates(api_client):
    user, patient = patient_user()
    document = awaiting(patient, user)
    first = make_candidate(document, date(2026, 3, 14), score=0.95, suggested=True)
    second = make_candidate(document, date(2026, 3, 12), score=0.4, suggested=False)
    authenticate(api_client, user)

    response = api_client.get(QUEUE)
    item = response.data["data"]["results"][0]
    assert len(item["detected_candidates"]) == 2
    # Ordered by score desc — both still exposed; the user picks.
    assert item["detected_candidates"][0]["uuid"] == first.uuid
    assert item["detected_candidates"][1]["uuid"] == second.uuid


def test_candidate_based_confirmation_removes_from_queue(api_client):
    user, patient = patient_user()
    document = awaiting(patient, user)
    candidate = make_candidate(document, date(2026, 3, 14), suggested=True)
    authenticate(api_client, user)

    assert api_client.get(QUEUE).data["data"]["count"] == 1

    response = api_client.post(
        confirm_endpoint(document),
        {"candidate_id": candidate.uuid},
        format="json",
    )
    assert response.status_code == 200
    assert response.data["data"]["date_source"] == "USER_CONFIRMED"

    # Confirmed doc leaves the queue; archive still contains it.
    assert api_client.get(QUEUE).data["data"]["count"] == 0
    archive = api_client.get(ARCHIVE).data["data"]
    assert archive["count"] == 1
    assert archive["unconfirmed_date_count"] == 0
    assert archive["results"][0]["uuid"] == str(document.uuid)


def test_manual_confirmation_removes_from_queue(api_client):
    user, patient = patient_user()
    document = awaiting(patient, user)
    authenticate(api_client, user)
    api_client.post(
        confirm_endpoint(document),
        {"date": "2026-03-14"},
        format="json",
    )
    assert api_client.get(QUEUE).data["data"]["count"] == 0


def test_count_list_consistency_across_endpoints(api_client):
    user, patient = patient_user()
    awaiting(patient, user)
    awaiting(patient, user)
    verified_document(patient, user, date(2026, 3, 14))
    authenticate(api_client, user)

    queue = api_client.get(QUEUE).data["data"]
    archive = api_client.get(ARCHIVE).data["data"]
    summary = api_client.get(SUMMARY).data["data"]
    assert queue["count"] == 2
    assert len(queue["results"]) == 2
    assert archive["unconfirmed_date_count"] == 2
    assert summary["unconfirmed_date_count"] == 2


def test_ownership_isolation_for_queue(api_client):
    user, patient = patient_user()
    other_user, other_patient = patient_user(
        email="queue-other@example.com", digital_id="76543210987654329"
    )
    awaiting(patient, user)
    other = awaiting(other_patient, other_user)
    authenticate(api_client, user)

    assert api_client.get(QUEUE).data["data"]["count"] == 1
    # Other patient cannot confirm this document.
    response = api_client.post(
        confirm_endpoint(other),
        {"date": "2026-03-14"},
        format="json",
    )
    assert response.status_code in (403, 404)


def test_archive_default_includes_unconfirmed_date_document(api_client):
    user, patient = patient_user()
    awaiting_doc = awaiting(patient, user)
    verified = verified_document(patient, user, date(2026, 3, 14))
    authenticate(api_client, user)

    response = api_client.get(ARCHIVE)
    body = response.data["data"]
    assert body["count"] == 2
    uuids = {r["uuid"] for r in body["results"]}
    assert uuids == {str(awaiting_doc.uuid), str(verified.uuid)}
    # Undated/awaiting sorts by created_at (after the dated doc by recency).
    assert body["results"][0]["uuid"] == str(awaiting_doc.uuid)


def test_archive_year_filter_keeps_undated_out_but_all_dates_shows_them(
    api_client,
):
    user, patient = patient_user()
    awaiting(patient, user)
    verified_document(patient, user, date(2026, 3, 14))
    authenticate(api_client, user)

    all_dates = api_client.get(ARCHIVE).data["data"]
    assert all_dates["count"] == 2

    year_2026 = api_client.get(f"{ARCHIVE}?year=2026").data["data"]
    assert year_2026["count"] == 1  # only the dated doc
