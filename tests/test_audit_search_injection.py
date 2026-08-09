import pytest
from django.db import connection
from django.test import override_settings

from accounts.models import User
from documents.models import MedicalDocument
from documents.services import create_medical_document
from tests.archive_helpers import make_facility
from tests.test_medical_document_services import actor_and_patient

pytestmark = [pytest.mark.django_db, pytest.mark.postgresql]

SEARCH_URL = "/api/v1/search/"


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only search injection tests")


def verified_document(patient, actor, tmp_path, *, facility_name=None):
    from datetime import date

    facility = make_facility(name=facility_name or "Synthetic Medical Center")
    from tests.archive_helpers import attach_text
    from tests.test_medical_documents_api import upload

    document = create_medical_document(
        patient=patient,
        actor=actor,
        upload=upload("search-inject.png"),
        metadata={
            "document_type": "LABORATORY",
            "title": "CBC Routine Panel",
            "healthcare_facility_id": str(facility.uuid),
        },
    )
    document.document_date = date(2026, 3, 14)
    document.date_verified = True
    document.date_source = MedicalDocument.DateSource.USER_CONFIRMED
    document.processing_status = MedicalDocument.ProcessingStatus.DATE_CONFIRMED
    document.save(
        update_fields=(
            "document_date",
            "date_verified",
            "date_source",
            "processing_status",
            "updated_at",
        )
    )
    attach_text(
        document,
        "Complete blood count CBC plus RADIOLOGY imaging report content",
    )
    return document


INJECTION_PAYLOADS = (
    "CBC & RADIOLOGY",
    "CBC | RADIOLOGY",
    "CBC ! RADIOLOGY",
    "CBC <-> RADIOLOGY",
    "CBC & ! ( RADIOLOGY )",
    "' OR 1=1 --",
    "'; DROP TABLE medical_document; --",
    "cbc:1000",
    "RADIOLOGY:*",
    '"CBC" & "RADIOLOGY"',
)


@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_search_query_operators_are_literal_and_patient_scoped(
    api_client, tmp_path, payload
):
    require_postgresql()
    actor, patient = actor_and_patient()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        document = verified_document(patient, actor, tmp_path)

    api_client.force_authenticate(user=actor)
    response = api_client.get(SEARCH_URL, {"q": payload})
    assert response.status_code == 200, response.content
    results = response.data["data"]["results"]
    uuids = {row["uuid"] for row in results}
    # No error, no cross-patient leakage, query never escalates.
    assert uuids <= {str(document.uuid)}
    assert response.data["data"]["count"] == len(results)


def test_search_query_never_exposes_other_patients(api_client, tmp_path):
    require_postgresql()
    from datetime import date

    from patients.models import PatientProfile

    actor, patient = actor_and_patient()
    other_actor = User.objects.create_user(
        email="other-search@example.com",
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    other_patient = PatientProfile.objects.create(
        user=other_actor,
        digital_id="76543210987654321",
        full_name="Other Owner",
        date_of_birth=date(1990, 1, 2),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        document = verified_document(patient, actor, tmp_path)
        verified_document(
            other_patient, other_actor, tmp_path, facility_name="Other Medical Center"
        )

    api_client.force_authenticate(user=actor)
    other_uuid = str(MedicalDocument.objects.get(patient=other_patient).uuid)
    for payload in ("CBC", "' OR 1=1 --", "RADIOLOGY:*", "%"):
        response = api_client.get(SEARCH_URL, {"q": payload})
        assert response.status_code == 200
        uuids = {row["uuid"] for row in response.data["data"]["results"]}
        # Never leaks another patient's document, for any payload.
        assert other_uuid not in uuids, payload
        assert uuids <= {str(document.uuid)}, payload


def test_search_query_too_long_is_rejected(api_client, tmp_path):
    require_postgresql()
    actor, patient = actor_and_patient()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        verified_document(patient, actor, tmp_path)
    api_client.force_authenticate(user=actor)
    long_payload = "A" * 250
    response = api_client.get(SEARCH_URL, {"q": long_payload})
    assert response.status_code == 400


def test_search_unknown_params_and_duplicate_q_rejected(api_client, tmp_path):
    require_postgresql()
    actor, patient = actor_and_patient()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        verified_document(patient, actor, tmp_path)
    api_client.force_authenticate(user=actor)
    unknown = api_client.get(SEARCH_URL, {"q": "CBC", "patient": "x"})
    assert unknown.status_code == 400
    bad_filters = api_client.get(
        SEARCH_URL,
        {
            "q": "CBC",
            "document_type": "not-a-type",
            "facility_name": "x",
            "from": "nonsense",
        },
    )
    assert bad_filters.status_code == 400


def test_search_injection_returns_controlled_response(api_client, tmp_path):
    """End-to-end: even hostile input yields 200/400, never 500."""
    require_postgresql()
    actor, patient = actor_and_patient()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        verified_document(patient, actor, tmp_path)
    api_client.force_authenticate(user=actor)
    for payload in INJECTION_PAYLOADS:
        response = api_client.get(SEARCH_URL, {"q": payload})
        assert response.status_code in (200, 400), payload
