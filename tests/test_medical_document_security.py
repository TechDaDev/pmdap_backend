import io
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from pypdf import PdfWriter

from accounts.models import User
from documents.exceptions import MedicalFileStorageFailed
from documents.models import MedicalDocument, StoredFile
from documents.services import create_medical_document, verify_stored_file_integrity
from patients.models import PatientProfile

pytestmark = pytest.mark.django_db

COLLECTION = "/api/v1/documents/"


def owner():
    user = User.objects.create_user(
        email="security-owner@example.com",
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    profile = PatientProfile.objects.create(
        user=user,
        digital_id="50000000000000001",
        full_name="Security Owner",
        date_of_birth=date(1990, 1, 1),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )
    return user, profile


def file_bytes(file_format):
    output = io.BytesIO()
    if file_format == "PDF":
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.write(output)
    else:
        Image.new("RGB", (2, 2), "teal").save(output, format=file_format)
    return output.getvalue()


@pytest.mark.parametrize(
    ("file_format", "name", "mime"),
    [
        ("PDF", "report.pdf", "application/pdf"),
        ("JPEG", "report.jpg", "image/jpeg"),
        ("PNG", "report.png", "image/png"),
    ],
)
def test_adult_endpoint_accepts_only_validated_supported_formats(
    api_client,
    tmp_path,
    file_format,
    name,
    mime,
):
    user, _ = owner()
    api_client.force_authenticate(user=user)
    medical_file = SimpleUploadedFile(name, file_bytes(file_format), content_type=mime)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        response = api_client.post(
            COLLECTION,
            {"file": medical_file, "document_type": "OTHER"},
            format="multipart",
        )

    assert response.status_code == 201
    assert response.data["data"]["file"]["mime_type"] == mime


def test_upload_validation_failure_uses_error_envelope(api_client, tmp_path):
    user, _ = owner()
    api_client.force_authenticate(user=user)
    malformed = SimpleUploadedFile(
        "malware.pdf",
        b"MZ executable",
        content_type="application/pdf",
    )
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        response = api_client.post(
            COLLECTION,
            {"file": malformed, "document_type": "OTHER"},
            format="multipart",
        )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "validation_error"
    assert "file" in response.data["error"]["details"]


def test_upload_throttle_has_consistent_error_envelope(
    api_client,
    tmp_path,
    settings,
):
    user, _ = owner()
    api_client.force_authenticate(user=user)
    rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    original = rates["medical_document_upload"]
    try:
        rates["medical_document_upload"] = "1/hour"
        with override_settings(MEDICAL_FILE_ROOT=tmp_path):
            first = api_client.post(
                COLLECTION,
                {
                    "file": SimpleUploadedFile(
                        "first.png", file_bytes("PNG"), content_type="image/png"
                    ),
                    "document_type": "OTHER",
                },
                format="multipart",
            )
            second = api_client.post(
                COLLECTION,
                {
                    "file": SimpleUploadedFile(
                        "second.jpg", file_bytes("JPEG"), content_type="image/jpeg"
                    ),
                    "document_type": "OTHER",
                },
                format="multipart",
            )
    finally:
        rates["medical_document_upload"] = original

    assert first.status_code == 201
    assert second.status_code == 429
    assert second.data["error"]["code"] == "throttled"


def test_storage_write_failure_leaves_no_usable_document(tmp_path):
    user, profile = owner()
    medical_file = SimpleUploadedFile(
        "report.png", file_bytes("PNG"), content_type="image/png"
    )
    with (
        override_settings(MEDICAL_FILE_ROOT=tmp_path),
        patch(
            "documents.storage.PrivateMedicalStorage._save",
            side_effect=OSError("storage unavailable"),
        ),
        pytest.raises(MedicalFileStorageFailed),
    ):
        create_medical_document(
            patient=profile,
            actor=user,
            upload=medical_file,
            metadata={"document_type": "OTHER"},
        )

    assert not MedicalDocument.objects.exists()
    assert not StoredFile.objects.exists()
    assert not [path for path in Path(tmp_path).rglob("*") if path.is_file()]


def test_identity_verification_agent_cannot_access_medical_detail(api_client, tmp_path):
    user, _ = owner()
    api_client.force_authenticate(user=user)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        created = api_client.post(
            COLLECTION,
            {
                "file": SimpleUploadedFile(
                    "report.png", file_bytes("PNG"), content_type="image/png"
                ),
                "document_type": "OTHER",
            },
            format="multipart",
        ).data["data"]

        agent = User.objects.create_user(
            email="medical-agent@example.com",
            password="A-complex-password-2026!",
            role=User.Role.IDENTITY_VERIFICATION_AGENT,
            status=User.Status.ACTIVE,
        )
        api_client.force_authenticate(user=agent)
        detail = api_client.get(f"{COLLECTION}{created['uuid']}/")
        file_response = api_client.get(f"{COLLECTION}{created['uuid']}/file/")

    assert detail.status_code == 403
    assert file_response.status_code == 403


def test_missing_private_blob_returns_stable_storage_error(api_client, tmp_path):
    user, _ = owner()
    api_client.force_authenticate(user=user)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        created = api_client.post(
            COLLECTION,
            {
                "file": SimpleUploadedFile(
                    "report.png", file_bytes("PNG"), content_type="image/png"
                ),
                "document_type": "OTHER",
            },
            format="multipart",
        ).data["data"]
        document = MedicalDocument.objects.get(uuid=created["uuid"])
        Path(document.stored_file.file.path).unlink()
        response = api_client.get(f"{COLLECTION}{created['uuid']}/file/")

    assert response.status_code == 503
    assert response.data["error"]["code"] == "medical_file_storage_failed"


def test_known_corrupted_file_is_not_streamed(api_client, tmp_path):
    user, _ = owner()
    api_client.force_authenticate(user=user)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        created = api_client.post(
            COLLECTION,
            {
                "file": SimpleUploadedFile(
                    "report.png", file_bytes("PNG"), content_type="image/png"
                ),
                "document_type": "OTHER",
            },
            format="multipart",
        ).data["data"]
        document = MedicalDocument.objects.select_related("stored_file").get(
            uuid=created["uuid"]
        )
        Path(document.stored_file.file.path).write_bytes(b"tampered")
        verify_stored_file_integrity(document.stored_file, actor=user)
        response = api_client.get(f"{COLLECTION}{created['uuid']}/file/")

    assert response.status_code == 409
    assert response.data["error"]["code"] == "medical_file_unavailable"
