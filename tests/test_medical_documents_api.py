import io
from datetime import date
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image

from accounts.models import User
from documents.models import MedicalDocument, MedicalDocumentEvent
from patients.models import PatientProfile

pytestmark = pytest.mark.django_db

COLLECTION = "/api/v1/documents/"


def png_bytes(color="purple"):
    output = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(output, format="PNG")
    return output.getvalue()


def upload(name="clinical.png", content=None):
    return SimpleUploadedFile(
        name,
        content or png_bytes(),
        content_type="image/png",
    )


def patient_user(*, email="patient@example.com", digital_id="12345678901234567"):
    user = User.objects.create_user(
        email=email,
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    profile = PatientProfile.objects.create(
        user=user,
        digital_id=digital_id,
        full_name="Adult Patient",
        date_of_birth=date(1990, 1, 2),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )
    return user, profile


def authenticate(client, user):
    client.force_authenticate(user=user)


def payload(**overrides):
    return {
        "file": upload(),
        "document_type": "LABORATORY",
        **overrides,
    }


def assert_error(response, status_code, code):
    assert response.status_code == status_code
    assert response.data["error"]["code"] == code
    assert set(response.data["error"]) == {"code", "message", "details"}


def test_adult_upload_derives_owner_and_accepts_optional_metadata(api_client, tmp_path):
    user, profile = patient_user()
    authenticate(api_client, user)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        response = api_client.post(
            COLLECTION,
            payload(
                title="CBC",
                description="Annual test",
                document_date="2026-08-01",
                facility_name="Central Lab",
                location_text="Baghdad",
                department="Hematology",
                physician_name="Dr Example",
            ),
            format="multipart",
        )

    assert response.status_code == 201
    document = MedicalDocument.objects.get()
    assert document.patient == profile
    assert document.uploaded_by == user
    assert response.data["data"]["title"] == "CBC"
    assert response.data["data"]["date_source"] == "USER_ENTERED"
    assert "patient" not in response.data["data"]
    encoded = str(response.data)
    for forbidden in ("sha256", "storage_key", str(tmp_path), "content_sha256"):
        assert forbidden not in encoded


@pytest.mark.parametrize(
    "protected",
    [
        {"patient": "00000000-0000-0000-0000-000000000000"},
        {"uploaded_by": "00000000-0000-0000-0000-000000000000"},
        {"stored_file": "00000000-0000-0000-0000-000000000000"},
        {"processing_status": "INDEXED"},
        {"archive_status": "DELETED"},
        {"date_verified": False},
        {"file.sha256": "a" * 64},
    ],
)
def test_upload_rejects_ownership_and_internal_field_injection(
    api_client,
    tmp_path,
    protected,
):
    user, _ = patient_user()
    authenticate(api_client, user)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        response = api_client.post(COLLECTION, payload(**protected), format="multipart")

    assert_error(response, 400, "validation_error")
    assert not MedicalDocument.objects.exists()


def test_list_and_detail_return_only_active_owned_documents(api_client, tmp_path):
    owner, _ = patient_user()
    other, _ = patient_user(email="other@example.com", digital_id="76543210987654321")
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        authenticate(api_client, owner)
        own_response = api_client.post(
            COLLECTION, payload(title="Own"), format="multipart"
        )
        own = own_response.data["data"]
        authenticate(api_client, other)
        other_uuid = api_client.post(
            COLLECTION,
            payload(title="Other", file=upload(content=png_bytes("orange"))),
            format="multipart",
        ).data["data"]["uuid"]
        authenticate(api_client, owner)

        listing = api_client.get(COLLECTION)
        detail = api_client.get(f"{COLLECTION}{own['uuid']}/")
        unrelated = api_client.get(f"{COLLECTION}{other_uuid}/")

    assert listing.status_code == 200
    assert listing.data["data"]["count"] == 1
    assert listing.data["data"]["results"][0]["uuid"] == own["uuid"]
    assert detail.status_code == 200
    assert_error(unrelated, 404, "medical_document_not_found")


def test_authorized_file_stream_is_original_and_header_safe(api_client, tmp_path):
    user, _ = patient_user()
    authenticate(api_client, user)
    content = png_bytes()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        created = api_client.post(
            COLLECTION,
            payload(file=upload("evil name.png", content)),
            format="multipart",
        ).data["data"]
        response = api_client.get(f"{COLLECTION}{created['uuid']}/file/")
        streamed = b"".join(response.streaming_content)

    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert streamed == content
    disposition = response["Content-Disposition"]
    assert "attachment" in disposition
    assert "\r" not in disposition and "\n" not in disposition
    assert str(tmp_path) not in disposition


def test_metadata_patch_sets_correction_provenance_and_rejects_protected_fields(
    api_client,
    tmp_path,
):
    user, _ = patient_user()
    authenticate(api_client, user)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        create_response = api_client.post(COLLECTION, payload(), format="multipart")
        created = create_response.data["data"]
        url = f"{COLLECTION}{created['uuid']}/"
        response = api_client.patch(
            url,
            {"title": "Corrected", "document_date": "2026-07-31"},
            format="json",
        )
        rejected = api_client.patch(
            url,
            {"patient": "00000000-0000-0000-0000-000000000000"},
            format="json",
        )

    assert response.status_code == 200
    assert response.data["data"]["date_source"] == "USER_CORRECTED"
    assert response.data["data"]["date_verified"] is True
    assert_error(rejected, 400, "validation_error")


def test_soft_delete_retains_blob_and_history_but_ends_normal_access(
    api_client,
    tmp_path,
):
    user, _ = patient_user()
    authenticate(api_client, user)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        create_response = api_client.post(COLLECTION, payload(), format="multipart")
        created = create_response.data["data"]
        url = f"{COLLECTION}{created['uuid']}/"
        document = MedicalDocument.objects.get(uuid=created["uuid"])
        stored_path = Path(document.stored_file.file.path)
        response = api_client.delete(url)
        repeat = api_client.delete(url)
        detail = api_client.get(url)
        file_response = api_client.get(f"{url}file/")
        listing = api_client.get(COLLECTION)

    document.refresh_from_db()
    assert response.status_code == 204
    assert_error(repeat, 404, "medical_document_not_found")
    assert_error(detail, 404, "medical_document_not_found")
    assert_error(file_response, 404, "medical_document_not_found")
    assert listing.data["data"]["count"] == 0
    assert document.archive_status == MedicalDocument.ArchiveStatus.DELETED
    assert document.deleted_at is not None and document.deleted_by == user
    assert stored_path.exists()
    assert document.events.filter(
        event_type=MedicalDocumentEvent.EventType.DELETED
    ).exists()


def test_duplicate_unauthenticated_agent_and_unsupported_methods_are_stable(
    api_client,
    tmp_path,
):
    unauthenticated = api_client.get(COLLECTION)
    assert_error(unauthenticated, 401, "not_authenticated")

    owner, _ = patient_user()
    authenticate(api_client, owner)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        create_response = api_client.post(COLLECTION, payload(), format="multipart")
        created = create_response.data["data"]
        duplicate = api_client.post(COLLECTION, payload(), format="multipart")
        unsupported = api_client.post(
            f"{COLLECTION}{created['uuid']}/", {}, format="json"
        )

    assert_error(duplicate, 409, "duplicate_medical_document")
    assert duplicate.data["error"]["details"] == {}
    assert "uuid" not in str(duplicate.data)
    assert_error(unsupported, 405, "method_not_allowed")

    agent = User.objects.create_user(
        email="agent@example.com",
        password="A-complex-password-2026!",
        role=User.Role.IDENTITY_VERIFICATION_AGENT,
        status=User.Status.ACTIVE,
    )
    authenticate(api_client, agent)
    denied = api_client.get(COLLECTION)
    assert denied.status_code == 403
