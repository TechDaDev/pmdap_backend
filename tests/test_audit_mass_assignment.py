import pytest
from django.test import override_settings

from documents.models import MedicalDocument
from tests.test_medical_documents_api import COLLECTION, patient_user
from tests.test_minors_guardians import create_verified_guardian

pytestmark = pytest.mark.django_db

# Endpoints and the protected fields an attacker might try to mass-assign.
# Each must be rejected as unknown/read-only (400), never silently accepted.


def assert_error(response):
    assert response.status_code == 400, response.content
    assert set(response.json()) == {"error"}


@pytest.mark.parametrize(
    "protected",
    [
        {"patient": "00000000-0000-0000-0000-000000000000"},
        {"uploaded_by": "00000000-0000-0000-0000-000000000000"},
        {"stored_file": "00000000-0000-0000-0000-000000000000"},
        {"processing_status": "INDEXED"},
        {"archive_status": "DELETED"},
        {"date_source": "PDF_EXTRACTION"},
        {"date_verified": True},
        {"date_verified_at": "2026-01-01T00:00:00Z"},
        {"created_at": "2026-01-01T00:00:00Z"},
        {"updated_at": "2026-01-01T00:00:00Z"},
    ],
)
def test_document_upload_rejects_protected_field_injection(
    api_client, tmp_path, protected
):
    user, _ = patient_user()
    api_client.force_authenticate(user=user)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        from tests.test_medical_documents_api import payload

        response = api_client.post(COLLECTION, payload(**protected), format="multipart")
    assert_error(response)
    assert not MedicalDocument.objects.exists()


@pytest.mark.parametrize(
    "protected",
    [
        {"patient": "00000000-0000-0000-0000-000000000000"},
        {"processing_status": "INDEXED"},
        {"archive_status": "DELETED"},
        {"date_source": "USER_ENTERED"},
        {"date_verified": True},
        {"date_verified_at": "2026-01-01T00:00:00Z"},
        {"classification_source": "USER"},
    ],
)
def test_document_update_rejects_protected_field_injection(
    api_client, tmp_path, protected
):
    user, _ = patient_user()
    api_client.force_authenticate(user=user)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        from tests.test_medical_documents_api import payload

        created = api_client.post(COLLECTION, payload(), format="multipart")
        assert created.status_code == 201, created.content
        url = f"{COLLECTION}{created.data['data']['uuid']}/"
        response = api_client.patch(url, protected, format="json")
    assert_error(response)


@pytest.mark.parametrize(
    "protected",
    [
        {"verification_status": "VERIFIED"},
        {"verified_by": "00000000-0000-0000-0000-000000000000"},
        {"verified_at": "2026-01-01T00:00:00Z"},
        {"patient": "00000000-0000-0000-0000-000000000000"},
    ],
)
def test_identity_submission_rejects_protected_field_injection(api_client, protected):
    user, profile, _ = create_verified_guardian()
    api_client.force_authenticate(user=user)
    from tests.test_minors_guardians import image_upload

    response = api_client.post(
        "/api/v1/identity-documents/",
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "document_number": "CARD-MASS-ASSIGN",
            "issuing_country": "IQ",
            "front_image": image_upload("mass-front.png"),
            **protected,
        },
        format="multipart",
    )
    assert_error(response)


@pytest.mark.parametrize(
    "protected",
    [
        {"verification_status": "VERIFIED"},
        {"verified_by": "00000000-0000-0000-0000-000000000000"},
        {"verified_at": "2026-01-01T00:00:00Z"},
        {"guardian": "00000000-0000-0000-0000-000000000000"},
    ],
)
def test_minor_creation_rejects_protected_field_injection(api_client, protected):
    user, profile, _ = create_verified_guardian()
    api_client.force_authenticate(user=user)
    from tests.test_minors_guardians import birth_document_payload

    response = api_client.post(
        "/api/v1/minors/",
        birth_document_payload(**protected),
        format="multipart",
    )
    assert_error(response)
