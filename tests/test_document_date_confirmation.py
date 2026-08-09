from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone

from accounts.models import User
from documents.models import DocumentDateEvent, MedicalDocument, StoredFile
from processing.date_services import process_date_candidates
from processing.models import DateCandidate
from tests.factories import UserFactory
from tests.test_date_processing import prepared_document
from tests.test_minor_medical_documents_api import (
    collection,
    minor,
    payload,
    relationship,
    verified_guardian,
)

pytestmark = pytest.mark.django_db


def ready_document(text="Report Date: 14/03/2026\nCollection Date: 12/03/2026"):
    document = prepared_document(text)
    assert (
        process_date_candidates(str(document.uuid))
        == MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION
    )
    document.refresh_from_db()
    return document


def endpoint(document):
    return f"/api/v1/documents/{document.uuid}/confirm-date/"


def authenticate_owner(api_client, document):
    api_client.force_authenticate(user=document.patient.user)


def test_owner_confirms_suggested_candidate(api_client):
    document = ready_document()
    candidate = document.date_candidates.get(is_current=True, is_suggested=True)
    authenticate_owner(api_client, document)

    response = api_client.post(
        endpoint(document), {"candidate_id": candidate.uuid}, format="json"
    )

    assert response.status_code == 200
    assert response.data["data"] == {
        "uuid": str(document.uuid),
        "document_date": "2026-03-14",
        "date_source": "USER_CONFIRMED",
        "date_verified": True,
        "date_verified_at": response.data["data"]["date_verified_at"],
        "processing_status": "DATE_CONFIRMED",
    }
    event = document.date_events.get()
    assert event.action == DocumentDateEvent.Action.DATE_CONFIRMED
    assert event.candidate_id == candidate.uuid
    assert event.actor == document.patient.user


def test_owner_can_choose_non_suggested_candidate_without_rewriting_score(api_client):
    document = ready_document()
    suggested = document.date_candidates.get(is_current=True, is_suggested=True)
    chosen = document.date_candidates.get(is_current=True, is_suggested=False)
    authenticate_owner(api_client, document)

    response = api_client.post(
        endpoint(document), {"candidate_id": chosen.uuid}, format="json"
    )

    assert response.status_code == 200
    assert response.data["data"]["document_date"] == "2026-03-12"
    suggested.refresh_from_db()
    chosen.refresh_from_db()
    assert suggested.is_suggested is True
    assert chosen.is_suggested is False


def test_manual_correction_requires_real_nonfuture_date(api_client):
    document = ready_document("Synthetic report contains no date expression.")
    authenticate_owner(api_client, document)

    malformed = api_client.post(
        endpoint(document), {"date": "31-02-2026"}, format="json"
    )
    future = api_client.post(
        endpoint(document),
        {"date": str(timezone.localdate() + timedelta(days=1))},
        format="json",
    )
    today = api_client.post(
        endpoint(document), {"date": str(timezone.localdate())}, format="json"
    )

    assert malformed.status_code == 400
    assert future.status_code == 400
    assert future.data["error"]["code"] == "invalid_document_date"
    assert today.status_code == 200
    assert today.data["data"]["date_source"] == "USER_CORRECTED"


@pytest.mark.parametrize("payload", [{}, {"candidate_id": None}, {"date": None}])
def test_request_requires_exactly_one_nonnull_decision(api_client, payload):
    document = ready_document()
    authenticate_owner(api_client, document)

    response = api_client.post(endpoint(document), payload, format="json")

    assert response.status_code == 400


def test_request_rejects_both_and_mass_assignment(api_client):
    document = ready_document()
    candidate = document.date_candidates.filter(is_current=True).first()
    authenticate_owner(api_client, document)

    both = api_client.post(
        endpoint(document),
        {"candidate_id": candidate.uuid, "date": "2026-03-10"},
        format="json",
    )
    injected = api_client.post(
        endpoint(document),
        {"date": "2026-03-10", "date_verified": False, "date_source": "OCR"},
        format="json",
    )

    assert both.status_code == 400
    assert both.data["error"]["code"] == "invalid_date_confirmation"
    assert injected.status_code == 400


def test_candidate_must_belong_to_document_and_be_current(api_client):
    document = ready_document()
    stored = StoredFile.objects.create(
        file="medical/foreign-date.pdf",
        original_filename="foreign-date.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        sha256="b" * 64,
        page_count=1,
        integrity_status=StoredFile.IntegrityStatus.VALID,
    )
    other = MedicalDocument.objects.create(
        patient=document.patient,
        uploaded_by=document.patient.user,
        stored_file=stored,
        content_sha256=stored.sha256,
        document_type=MedicalDocument.DocumentType.MEDICAL_REPORT,
        processing_status=MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION,
    )
    foreign = DateCandidate.objects.create(
        document=other,
        detected_date=date(2026, 3, 10),
        raw_value="10/03/2026",
        normalized_value="10/03/2026",
        candidate_type=DateCandidate.CandidateType.ISSUE_DATE,
        score=0.9,
        page_number=1,
        context="Issue Date: 10/03/2026",
        source=DateCandidate.Source.PDF_TEXT,
        occurrence_index=1,
        parsing_rule="DMY_NUMERIC",
        pipeline_version="m9-date-v1",
    )
    stale = document.date_candidates.filter(is_current=True).first()
    DateCandidate.objects.filter(pk=stale.pk).update(is_current=False)
    authenticate_owner(api_client, document)

    cross_document = api_client.post(
        endpoint(document), {"candidate_id": foreign.uuid}, format="json"
    )
    stale_response = api_client.post(
        endpoint(document), {"candidate_id": stale.uuid}, format="json"
    )

    assert cross_document.status_code == 404
    assert cross_document.data["error"]["code"] == "date_candidate_not_found"
    assert stale_response.status_code == 409
    assert stale_response.data["error"]["code"] == "date_candidate_stale"


def test_replay_is_idempotent_and_later_correction_is_audited(api_client):
    document = ready_document()
    candidate = document.date_candidates.get(is_current=True, is_suggested=True)
    authenticate_owner(api_client, document)

    first = api_client.post(
        endpoint(document), {"candidate_id": candidate.uuid}, format="json"
    )
    replay = api_client.post(
        endpoint(document), {"candidate_id": candidate.uuid}, format="json"
    )
    corrected = api_client.post(
        endpoint(document), {"date": "2026-03-10"}, format="json"
    )

    assert first.status_code == replay.status_code == corrected.status_code == 200
    assert document.date_events.count() == 2
    events = list(document.date_events.all())
    assert events[0].action == DocumentDateEvent.Action.DATE_CONFIRMED
    assert events[1].action == DocumentDateEvent.Action.DATE_CORRECTED
    assert events[1].previous_date == date(2026, 3, 14)
    assert events[1].new_date == date(2026, 3, 10)


def test_date_event_is_immutable(api_client):
    document = ready_document()
    authenticate_owner(api_client, document)
    api_client.post(endpoint(document), {"date": "2026-03-10"}, format="json")
    event = document.date_events.get()

    event.new_date = date(2026, 3, 11)
    with pytest.raises(ValidationError):
        event.save()
    with pytest.raises(ValidationError):
        event.delete()


def test_reprocessing_preserves_verified_date_and_history(api_client):
    document = ready_document()
    authenticate_owner(api_client, document)
    api_client.post(endpoint(document), {"date": "2026-03-10"}, format="json")
    prior_candidates = set(document.date_candidates.values_list("uuid", flat=True))

    outcome = process_date_candidates(str(document.uuid), reprocess=True)

    document.refresh_from_db()
    assert outcome == MedicalDocument.ProcessingStatus.DATE_CONFIRMED
    assert document.document_date == date(2026, 3, 10)
    assert document.date_source == MedicalDocument.DateSource.USER_CORRECTED
    assert document.date_events.count() == 1
    assert not document.date_candidates.filter(
        uuid__in=prior_candidates, is_current=True
    ).exists()
    assert document.date_candidates.filter(uuid__in=prior_candidates).count() == len(
        prior_candidates
    )


def test_manual_date_allowed_after_automatic_failure(api_client):
    document = prepared_document("Synthetic failed date extraction")
    document.processing_status = MedicalDocument.ProcessingStatus.FAILED
    document.save(update_fields=("processing_status", "updated_at"))
    authenticate_owner(api_client, document)

    response = api_client.post(
        endpoint(document), {"date": "2026-03-10"}, format="json"
    )

    assert response.status_code == 200
    assert response.data["data"]["processing_status"] == "DATE_CONFIRMED"


def test_unready_state_is_rejected(api_client):
    document = prepared_document("Synthetic unready document")
    document.processing_status = MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
    document.save(update_fields=("processing_status", "updated_at"))
    authenticate_owner(api_client, document)

    response = api_client.post(
        endpoint(document), {"date": "2026-03-10"}, format="json"
    )

    assert response.status_code == 409
    assert response.data["error"]["code"] == "invalid_date_confirmation_state"


def test_access_is_idor_safe_and_response_is_allowlisted(api_client):
    document = ready_document()
    unrelated = UserFactory(status=User.Status.ACTIVE)
    agent = UserFactory(
        role=User.Role.IDENTITY_VERIFICATION_AGENT,
        status=User.Status.ACTIVE,
    )
    url = endpoint(document)

    api_client.force_authenticate(user=unrelated)
    denied = api_client.post(url, {"date": "2026-03-10"}, format="json")
    api_client.force_authenticate(user=agent)
    agent_denied = api_client.post(url, {"date": "2026-03-10"}, format="json")
    api_client.force_authenticate(user=None)
    anonymous = api_client.post(url, {"date": "2026-03-10"}, format="json")

    assert denied.status_code == 404
    assert agent_denied.status_code == 403
    assert anonymous.status_code == 401


def test_soft_deleted_document_is_not_mutable(api_client):
    document = ready_document()
    document.archive_status = MedicalDocument.ArchiveStatus.DELETED
    document.save(update_fields=("archive_status", "updated_at"))
    authenticate_owner(api_client, document)

    response = api_client.post(
        endpoint(document), {"date": "2026-03-10"}, format="json"
    )

    assert response.status_code == 404


def test_confirm_endpoint_is_post_only(api_client):
    document = ready_document()
    authenticate_owner(api_client, document)

    assert api_client.get(endpoint(document)).status_code == 405
    assert api_client.patch(endpoint(document), {}, format="json").status_code == 405
    assert api_client.delete(endpoint(document)).status_code == 405


def test_live_guardian_can_correct_minor_date_but_ageout_is_denied(
    api_client, tmp_path
):
    guardian = verified_guardian(
        email="m10-guardian@example.com", digital_id="10000000000001001"
    )
    patient = minor(digital_id="30000000000001001")
    relationship(guardian, patient)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        api_client.force_authenticate(user=guardian)
        created = api_client.post(collection(patient), payload(), format="multipart")
    document = patient.medical_documents.get()
    document.processing_status = MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION
    document.save(update_fields=("processing_status", "updated_at"))
    url = f"{collection(patient)}{created.data['data']['uuid']}/confirm-date/"

    allowed = api_client.post(url, {"date": "2026-03-10"}, format="json")
    today = timezone.localdate()
    patient.date_of_birth = date(today.year - 18, today.month, today.day)
    patient.save(update_fields=("date_of_birth", "updated_at"))
    denied = api_client.post(url, {"date": "2026-03-11"}, format="json")

    assert allowed.status_code == 200
    assert denied.status_code == 404
