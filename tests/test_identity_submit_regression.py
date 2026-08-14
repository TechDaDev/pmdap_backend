"""Regression: reproduce exactly what the mobile client sends for identity
final submission and assert the real backend error envelope.

The dio client builds parts via MultipartFile.fromFile(...):

  * extraction   -> filename = basename(path)  (e.g. scan_page_1.jpg, photo.png)
  * final submit -> filename = 'front.jpg'/'back.jpg' (hardcoded)

Verified dio emission (see tool/repro_multipart.dart):
  * scan_page_*.jpg  -> content_type image/jpeg (JPEG bytes)  -> OK
  * photo.png with hardcoded 'front.jpg'
                     -> content_type image/jpeg (PNG bytes)    -> MISMATCH
  * no-extension file -> content_type application/octet-stream -> REJECTED

These tests feed the exact UploadedFile the backend would receive and assert
the sanitized envelope shape the app must handle: HTTP status + error.code +
error.message + error.details (field names only).
"""
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from tests.factories import UserFactory

COLLECTION = "/api/v1/identity-documents/"


def identity_document_model():
    from django.apps import apps

    return apps.get_model("identities", "IdentityDocument")


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    settings.IDENTITY_FILE_MAX_BYTES = 1024 * 1024


def auth(api_client, user):
    from rest_framework_simplejwt.tokens import RefreshToken

    token = str(RefreshToken.for_user(user).access_token)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")


def create_patient(*, email="patient@example.com"):
    from patients.services import create_patient_profile

    user = UserFactory(email=email, status="ACTIVE")
    profile = create_patient_profile(
        user=user,
        full_name="Layla Hassan",
        date_of_birth="1992-02-29",
        sex="FEMALE",
        nationality="IQ",
        blood_group="A+",
    )
    return user, profile


def png_bytes():
    out = io.BytesIO()
    Image.new("RGB", (8, 8), color=(35, 80, 120)).save(out, format="PNG")
    return out.getvalue()


def jpeg_bytes():
    out = io.BytesIO()
    Image.new("RGB", (8, 8), color=(35, 80, 120)).save(out, format="JPEG")
    return out.getvalue()


def payload(front_upload, back_upload=None, **overrides):
    data = {
        "document_type": "UNIFIED_NATIONAL_CARD",
        "document_number": "CARD-001",
        "national_number": "NAT-001",
        "family_number": "FAM-001",
        "issuing_country": "IQ",
        "front_image": front_upload,
        "back_image": back_upload or front_upload,
    }
    data.update(overrides)
    return data


def error_envelope(response):
    """Sanitized capture: status + code + message + detail field names only."""
    body = response.json()
    error = body.get("error", {})
    return {
        "status": response.status_code,
        "code": error.get("code"),
        "message": error.get("message"),
        "detail_fields": sorted(
            error.get("details", {}).keys()
        ) if isinstance(error.get("details"), dict) else error.get("details"),
    }


@pytest.mark.django_db
def test_png_bytes_with_hardcoded_jpg_filename_rejected(api_client):
    """dio PNG gallery pick -> filename 'front.jpg' -> content_type image/jpeg
    while PIL sees PNG -> 'Declared MIME type does not match image content'."""
    user, _ = create_patient()
    auth(api_client, user)

    upload = SimpleUploadedFile(
        "front.jpg", png_bytes(), content_type="image/jpeg"
    )
    response = api_client.post(
        COLLECTION, payload(upload), format="multipart"
    )

    captured = error_envelope(response)
    assert captured["status"] == 400
    assert captured["code"] == "validation_error"
    assert captured["message"] == "Validation failed."
    assert "front_image" in captured["detail_fields"]
    # Assert the exact server-side reason is reachable (single message text).
    detail_messages = response.json()["error"]["details"]["front_image"]
    assert any(
        "does not match image content" in str(m) for m in detail_messages
    )
    assert not identity_document_model().objects.exists()


@pytest.mark.django_db
def test_octet_stream_content_type_rejected(api_client):
    """dio no-extension file -> content_type application/octet-stream."""
    user, _ = create_patient()
    auth(api_client, user)

    upload = SimpleUploadedFile(
        "front.jpg", jpeg_bytes(), content_type="application/octet-stream"
    )
    response = api_client.post(
        COLLECTION, payload(upload), format="multipart"
    )

    captured = error_envelope(response)
    assert captured["status"] == 400
    assert captured["code"] == "validation_error"
    detail_messages = response.json()["error"]["details"]["front_image"]
    assert any("must be JPEG or PNG" in str(m) for m in detail_messages)
    assert not identity_document_model().objects.exists()


@pytest.mark.django_db
def test_valid_jpeg_scan_passes(api_client):
    """The physical scan path (JPEG + .jpg filename) must keep working."""
    user, _ = create_patient()
    auth(api_client, user)

    front = SimpleUploadedFile("front.jpg", jpeg_bytes(), content_type="image/jpeg")
    back = SimpleUploadedFile("back.jpg", jpeg_bytes(), content_type="image/jpeg")
    response = api_client.post(
        COLLECTION, payload(front, back), format="multipart"
    )

    assert response.status_code == 201
    assert identity_document_model().objects.exists()


@pytest.mark.django_db
def test_second_pending_national_card_returns_409_conflict(api_client):
    """A CURRENT PENDING document of the same type → 409 identity_document
    _conflict, which the app must map to the replace/review UX (never retry).
    """
    user, _ = create_patient()
    auth(api_client, user)

    first = SimpleUploadedFile("front.jpg", jpeg_bytes(), content_type="image/jpeg")
    first_back = SimpleUploadedFile(
        "back.jpg", jpeg_bytes(), content_type="image/jpeg"
    )
    first_resp = api_client.post(COLLECTION, payload(first, first_back), format="multipart")
    assert first_resp.status_code == 201

    second = SimpleUploadedFile("front.jpg", jpeg_bytes(), content_type="image/jpeg")
    second_back = SimpleUploadedFile(
        "back.jpg", jpeg_bytes(), content_type="image/jpeg"
    )
    response = api_client.post(COLLECTION, payload(second, second_back), format="multipart")

    captured = error_envelope(response)
    assert captured["status"] == 409
    assert captured["code"] == "identity_document_conflict"
    assert captured["message"] == (
        "Use the explicit replacement workflow for this document type."
    )
    assert captured["detail_fields"] == []


def _submit(api_client, *, document_number="CARD-002", family_number="FAM-002"):
    front = SimpleUploadedFile("front.jpg", jpeg_bytes(), content_type="image/jpeg")
    back = SimpleUploadedFile("back.jpg", jpeg_bytes(), content_type="image/jpeg")
    return api_client.post(
        COLLECTION,
        payload(
            front,
            back,
            document_number=document_number,
            national_number=document_number,
            family_number=family_number,
        ),
        format="multipart",
    )


@pytest.mark.django_db
def test_second_verified_national_card_returns_409(api_client):
    """CURRENT + VERIFIED card still blocks a normal duplicate submission."""
    from accounts.models import User

    user, _ = create_patient()
    auth(api_client, user)
    assert _submit(api_client).status_code == 201

    agent = UserFactory(role=User.Role.IDENTITY_VERIFICATION_AGENT)
    from identities.services import approve_identity_document

    doc = identity_document_model().objects.get(patient__user=user)
    approve_identity_document(document=doc, agent=agent)
    doc.refresh_from_db()
    assert doc.verification_status == identity_document_model().VerificationStatus.VERIFIED

    captured = error_envelope(_submit(api_client, document_number="CARD-003"))
    assert captured["status"] == 409
    assert captured["code"] == "identity_document_conflict"
    assert identity_document_model().objects.filter(patient__user=user).count() == 1


@pytest.mark.django_db
def test_family_number_does_not_bypass_pending_conflict(api_client):
    """Family number is irrelevant to the lock: a different family number on
    the duplicate still hits 409 while a CURRENT PENDING card exists."""
    user, _ = create_patient()
    auth(api_client, user)
    assert _submit(api_client, family_number="FAM-001").status_code == 201

    captured = error_envelope(
        _submit(api_client, document_number="CARD-003", family_number="DIFFERENT-FAM")
    )
    assert captured["status"] == 409
    assert captured["code"] == "identity_document_conflict"


@pytest.mark.django_db
def test_rejected_card_allows_resubmission(api_client):
    """REJECTED (no current PENDING/VERIFIED replacement) → normal submit is
    allowed again; the new card becomes CURRENT+PENDING and locks again."""
    from accounts.models import User

    user, _ = create_patient()
    auth(api_client, user)
    assert _submit(api_client).status_code == 201

    agent = UserFactory(role=User.Role.IDENTITY_VERIFICATION_AGENT)
    from identities.services import reject_identity_document

    doc = identity_document_model().objects.get(patient__user=user)
    reject_identity_document(document=doc, agent=agent, reason="synthetic test")
    doc.refresh_from_db()
    assert doc.verification_status == identity_document_model().VerificationStatus.REJECTED

    # Resubmission allowed while REJECTED.
    assert _submit(api_client, document_number="CARD-003").status_code == 201
    docs = identity_document_model().objects.filter(patient__user=user)
    assert docs.count() == 2
    new_doc = docs.get(document_number="CARD-003")
    assert new_doc.status == identity_document_model().LifecycleStatus.CURRENT
    assert new_doc.verification_status == identity_document_model().VerificationStatus.PENDING

    # Locked again after the fresh PENDING card.
    captured = error_envelope(_submit(api_client, document_number="CARD-004"))
    assert captured["status"] == 409
    assert captured["code"] == "identity_document_conflict"
