from datetime import date

import pytest
from django.test import override_settings
from django.utils import timezone

from accounts.models import User
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


def candidate(document, *, context="Report Date: 14/03/2026"):
    return DateCandidate.objects.create(
        document=document,
        detected_date=date(2026, 3, 14),
        raw_value="14/03/2026",
        normalized_value="14/03/2026",
        candidate_type=DateCandidate.CandidateType.REPORT_DATE,
        score=0.98,
        page_number=1,
        context=context,
        source=DateCandidate.Source.PDF_TEXT,
        occurrence_index=13,
        parsing_rule="DMY_NUMERIC",
        pipeline_version="m9-date-v2",
        is_suggested=True,
    )


def test_owner_gets_only_allowlisted_paginated_candidate_fields(api_client):
    document = prepared_document("Report Date: 14/03/2026")
    candidate(document)
    api_client.force_authenticate(user=document.patient.user)

    response = api_client.get(f"/api/v1/documents/{document.uuid}/date-candidates/")

    assert response.status_code == 200
    assert response.data["data"]["count"] == 1
    item = response.data["data"]["results"][0]
    assert set(item) == {
        "uuid",
        "date",
        "alternative_date",
        "type",
        "score",
        "page_number",
        "context",
        "source",
        "ambiguous",
        "is_suggested",
    }
    assert item["date"] == "2026-03-14"
    assert item["type"] == "REPORT_DATE"
    assert item["is_suggested"] is True
    assert "raw_value" not in item
    assert "pipeline_version" not in item
    assert "parsing_rule" not in item


def test_candidate_endpoint_is_idor_safe_and_get_only(api_client):
    document = prepared_document("Report Date: 14/03/2026")
    candidate(document)
    unrelated = UserFactory(status=User.Status.ACTIVE)
    url = f"/api/v1/documents/{document.uuid}/date-candidates/"

    api_client.force_authenticate(user=unrelated)
    denied = api_client.get(url)
    api_client.force_authenticate(user=document.patient.user)
    unsupported = api_client.post(url, {}, format="json")
    api_client.force_authenticate(user=None)
    anonymous = api_client.get(url)

    assert denied.status_code == 404
    assert unsupported.status_code == 405
    assert anonymous.status_code == 401


def test_verification_agent_has_zero_candidate_access(api_client):
    document = prepared_document("Report Date: 14/03/2026")
    candidate(document)
    agent = UserFactory(
        email="date-verification-agent@example.com",
        role=User.Role.IDENTITY_VERIFICATION_AGENT,
        status=User.Status.ACTIVE,
    )
    api_client.force_authenticate(user=agent)

    response = api_client.get(f"/api/v1/documents/{document.uuid}/date-candidates/")

    assert response.status_code == 403


def test_candidate_context_cannot_inject_response_headers(api_client):
    document = prepared_document("Report Date: 14/03/2026")
    candidate(document, context="Report Date: 14/03/2026\r\nX-Date-Leak: yes")
    api_client.force_authenticate(user=document.patient.user)

    response = api_client.get(f"/api/v1/documents/{document.uuid}/date-candidates/")

    assert response.status_code == 200
    assert "X-Date-Leak" not in response.headers
    assert "X-Date-Leak" in response.data["data"]["results"][0]["context"]


def test_guardian_candidate_route_reuses_live_minor_authorization(api_client, tmp_path):
    guardian = verified_guardian(
        email="date-guardian@example.com", digital_id="10000000000000801"
    )
    patient = minor(digital_id="30000000000000801")
    relationship(guardian, patient)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        api_client.force_authenticate(user=guardian)
        created = api_client.post(collection(patient), payload(), format="multipart")
    document = patient.medical_documents.get()
    candidate(document)
    url = f"{collection(patient)}{created.data['data']['uuid']}/date-candidates/"

    allowed = api_client.get(url)
    pending = verified_guardian(
        email="date-pending@example.com", digital_id="10000000000000802"
    )
    relationship(pending, patient, status="PENDING", active=False, kind="MOTHER")
    api_client.force_authenticate(user=pending)
    pending_denied = api_client.get(url)
    rejected = verified_guardian(
        email="date-rejected@example.com", digital_id="10000000000000803"
    )
    relationship(
        rejected,
        patient,
        status="REJECTED",
        active=False,
        kind="LEGAL_GUARDIAN",
    )
    api_client.force_authenticate(user=rejected)
    rejected_denied = api_client.get(url)
    unrelated = verified_guardian(
        email="date-unrelated@example.com", digital_id="10000000000000804"
    )
    api_client.force_authenticate(user=unrelated)
    unrelated_denied = api_client.get(url)
    today = timezone.localdate()
    patient.date_of_birth = date(today.year - 18, today.month, today.day)
    patient.save(update_fields=("date_of_birth", "updated_at"))
    api_client.force_authenticate(user=guardian)
    ageout_denied = api_client.get(url)

    assert allowed.status_code == 200
    for response in (
        pending_denied,
        rejected_denied,
        unrelated_denied,
        ageout_denied,
    ):
        assert response.status_code == 404
