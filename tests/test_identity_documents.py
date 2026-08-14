import hashlib
import io
import uuid
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.apps import apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from PIL import Image
from rest_framework_simplejwt.tokens import RefreshToken

from tests.factories import UserFactory

COLLECTION = "/api/v1/identity-documents/"
VERIFY_COLLECTION = "/api/v1/verification/identity-documents/"
PROFILE_INPUT = {
    "full_name": "Layla Hassan",
    "date_of_birth": "1992-02-29",
    "sex": "FEMALE",
    "nationality": "IQ",
    "blood_group": "A+",
}
SUMMARY_FIELDS = {
    "uuid",
    "document_type",
    "issuing_country",
    "issue_date",
    "expiry_date",
    "verification_status",
    "status",
    "created_at",
    "updated_at",
}
DETAIL_FIELDS = SUMMARY_FIELDS | {
    "document_number",
    "national_number",
    "family_number",
    "unique_card_body_number",
    "verified_at",
    "rejection_reason",
    "available_images",
    "replaces",
}


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    settings.IDENTITY_FILE_MAX_BYTES = 1024 * 1024


def identity_document_model():
    return apps.get_model("identities", "IdentityDocument")


def identity_file_model():
    return apps.get_model("identities", "IdentityFile")


def identity_event_model():
    return apps.get_model("identities", "IdentityDocumentEvent")


def auth(api_client, user):
    token = str(RefreshToken.for_user(user).access_token)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")


def create_patient(*, email="patient@example.com"):
    from patients.services import create_patient_profile

    user = UserFactory(email=email, status="ACTIVE")
    profile = create_patient_profile(user=user, **PROFILE_INPUT)
    return user, profile


def image_bytes(image_format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (8, 8), color=(35, 80, 120)).save(output, format=image_format)
    return output.getvalue()


def image_upload(name="identity.png", image_format="PNG", content_type=None):
    raw = image_bytes(image_format)
    mime = content_type or ("image/png" if image_format == "PNG" else "image/jpeg")
    return SimpleUploadedFile(name, raw, content_type=mime)


def national_card_payload(**overrides):
    payload = {
        "document_type": "UNIFIED_NATIONAL_CARD",
        "document_number": "CARD-001",
        "national_number": "NAT-001",
        "family_number": "FAM-001",
        "issuing_country": "IQ",
        "issue_date": "2022-01-01",
        "expiry_date": "2032-01-01",
        "front_image": image_upload("front.png"),
        "back_image": image_upload("back.png"),
    }
    payload.update(overrides)
    return payload


def passport_payload(**overrides):
    payload = {
        "document_type": "PASSPORT",
        "document_number": "P1234567",
        "issuing_country": "IQ",
        "issue_date": "2024-01-01",
        "expiry_date": "2034-01-01",
        "front_image": image_upload("passport.jpg", "JPEG"),
    }
    payload.update(overrides)
    return payload


def assert_error(response, status_code, code=None):
    assert response.status_code == status_code
    assert set(response.json()) == {"error"}
    error = response.json()["error"]
    assert set(error) == {"code", "message", "details"}
    if code:
        assert error["code"] == code


def submit(api_client, user, payload=None):
    auth(api_client, user)
    return api_client.post(
        COLLECTION, payload or national_card_payload(), format="multipart"
    )


def approve(api_client, agent, document_uuid):
    auth(api_client, agent)
    return api_client.post(
        f"{VERIFY_COLLECTION}{document_uuid}/approve/", {}, format="json"
    )


@pytest.mark.django_db
def test_valid_national_card_submission_creates_owned_pending_document(api_client):
    user, profile = create_patient()

    response = submit(api_client, user)

    assert response.status_code == 201
    document = identity_document_model().objects.get()
    profile.refresh_from_db()
    assert document.patient == profile
    assert document.document_type == "UNIFIED_NATIONAL_CARD"
    assert document.document_number == "CARD-001"
    assert document.national_number == "NAT-001"
    assert document.family_number == "FAM-001"
    assert document.issuing_country == "IQ"
    assert document.verification_status == "PENDING"
    assert document.status == "CURRENT"
    assert profile.identity_status == "PENDING_VERIFICATION"
    assert set(response.json()["data"]) == DETAIL_FIELDS


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field",
    [
        "document_number",
        "national_number",
        "family_number",
        "front_image",
        "back_image",
    ],
)
def test_national_card_missing_required_field_rejected(api_client, field):
    user, _ = create_patient()
    payload = national_card_payload()
    payload.pop(field)

    response = submit(api_client, user, payload)

    assert_error(response, 400, "validation_error")
    assert field in response.json()["error"]["details"]
    assert not identity_document_model().objects.exists()


@pytest.mark.django_db
def test_national_card_requires_iraqi_issuing_country(api_client):
    user, _ = create_patient()

    response = submit(api_client, user, national_card_payload(issuing_country="US"))

    assert_error(response, 400, "validation_error")
    assert "issuing_country" in response.json()["error"]["details"]


@pytest.mark.django_db
def test_passport_submission_supported_without_back_image(api_client):
    user, profile = create_patient()

    response = submit(api_client, user, passport_payload())

    assert response.status_code == 201
    document = identity_document_model().objects.get()
    profile.refresh_from_db()
    assert document.document_type == "PASSPORT"
    assert document.back_image_id is None
    assert profile.identity_status == "UNVERIFIED"


@pytest.mark.django_db
def test_birth_document_model_and_api_support(api_client):
    user, _ = create_patient()
    payload = {
        "document_type": "BIRTH_DOCUMENT",
        "document_number": "BIRTH-9",
        "issuing_country": "IQ",
        "issue_date": "1992-03-01",
        "front_image": image_upload("birth.png"),
    }

    response = submit(api_client, user, payload)

    assert response.status_code == 201
    assert identity_document_model().objects.get().document_type == "BIRTH_DOCUMENT"


@pytest.mark.django_db
def test_invalid_document_type_rejected(api_client):
    user, _ = create_patient()

    response = submit(
        api_client, user, national_card_payload(document_type="DRIVING_LICENSE")
    )

    assert_error(response, 400, "validation_error")
    assert "document_type" in response.json()["error"]["details"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("patient", "8bd01c1c-7ab4-4a8b-b821-40ab006d74c7"),
        ("verification_status", "VERIFIED"),
        ("verified_by", "8bd01c1c-7ab4-4a8b-b821-40ab006d74c7"),
        ("verified_at", "2026-01-01T00:00:00Z"),
        ("rejection_reason", "trusted"),
        ("status", "REVOKED"),
        ("sha256", "0" * 64),
    ],
)
def test_patient_submission_rejects_mass_assignment(api_client, field, value):
    user, _ = create_patient()

    response = submit(api_client, user, national_card_payload(**{field: value}))

    assert_error(response, 400, "validation_error")
    assert field in response.json()["error"]["details"]


@pytest.mark.django_db
def test_document_uuid_and_patient_identifiers_remain_immutable(api_client):
    from django.core.exceptions import ValidationError

    user, profile = create_patient()
    original_patient_uuid = profile.uuid
    original_digital_id = profile.digital_id
    response = submit(api_client, user)
    document = identity_document_model().objects.get()
    document.uuid = uuid.uuid4()

    with pytest.raises(ValidationError, match="immutable"):
        document.save()

    profile.refresh_from_db()
    assert profile.uuid == original_patient_uuid
    assert profile.digital_id == original_digital_id
    assert response.status_code == 201


@pytest.mark.django_db
def test_list_is_owner_scoped_and_uses_non_sensitive_projection(api_client):
    owner, _ = create_patient(email="owner@example.com")
    other, _ = create_patient(email="other@example.com")
    submit(api_client, owner)
    submit(api_client, other, passport_payload(document_number="OTHER-PASS"))

    auth(api_client, owner)
    response = api_client.get(COLLECTION)

    assert response.status_code == 200
    assert response.json()["data"]["count"] == 1
    item = response.json()["data"]["results"][0]
    assert set(item) == SUMMARY_FIELDS
    assert "family_number" not in item
    assert "OTHER-PASS" not in response.content.decode()


@pytest.mark.django_db
def test_detail_and_file_are_owner_scoped_against_idor(api_client):
    owner, _ = create_patient(email="owner@example.com")
    attacker, _ = create_patient(email="attacker@example.com")
    created = submit(api_client, owner).json()["data"]
    auth(api_client, attacker)

    detail = api_client.get(f"{COLLECTION}{created['uuid']}/")
    image = api_client.get(f"{COLLECTION}{created['uuid']}/images/front/")

    assert_error(detail, 404, "not_found")
    assert_error(image, 404, "not_found")
    assert "FAM-001" not in detail.content.decode()


@pytest.mark.django_db
def test_identity_endpoints_require_authentication(api_client):
    unknown = uuid.uuid4()

    for method, path in [
        (api_client.get, COLLECTION),
        (api_client.post, COLLECTION),
        (api_client.get, f"{COLLECTION}{unknown}/"),
        (api_client.get, f"{COLLECTION}{unknown}/images/front/"),
        (api_client.post, f"{COLLECTION}{unknown}/replace/"),
        (api_client.get, VERIFY_COLLECTION),
    ]:
        response = method(path)
        assert_error(response, 401, "not_authenticated")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("name", "raw", "content_type", "expected_field"),
    [
        ("front.gif", b"GIF89a", "image/gif", "front_image"),
        ("front.png", b"", "image/png", "front_image"),
        ("front.png", b"not an image", "image/png", "front_image"),
        ("front.png", b"MZ" + b"\x00" * 256, "image/png", "front_image"),
    ],
)
def test_invalid_identity_image_rejected(
    api_client, name, raw, content_type, expected_field
):
    user, _ = create_patient()
    upload = SimpleUploadedFile(name, raw, content_type=content_type)

    response = submit(api_client, user, national_card_payload(front_image=upload))

    assert_error(response, 400, "validation_error")
    assert expected_field in response.json()["error"]["details"]
    assert not identity_file_model().objects.exists()


@pytest.mark.django_db
def test_declared_mime_must_match_actual_image(api_client):
    user, _ = create_patient()
    upload = SimpleUploadedFile(
        "front.jpg", image_bytes("PNG"), content_type="image/jpeg"
    )

    response = submit(api_client, user, national_card_payload(front_image=upload))

    assert_error(response, 400, "validation_error")
    assert "front_image" in response.json()["error"]["details"]


@pytest.mark.django_db
def test_oversized_identity_image_rejected(api_client, settings):
    user, _ = create_patient()
    settings.IDENTITY_FILE_MAX_BYTES = len(image_bytes()) - 1

    response = submit(api_client, user)

    assert_error(response, 400, "validation_error")
    assert not identity_file_model().objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("image_format", "mime"), [("PNG", "image/png"), ("JPEG", "image/jpeg")]
)
def test_original_bytes_and_sha256_are_preserved(api_client, image_format, mime):
    user, _ = create_patient()
    raw = image_bytes(image_format)
    upload = SimpleUploadedFile(f"front.{image_format.lower()}", raw, content_type=mime)

    response = submit(api_client, user, national_card_payload(front_image=upload))

    assert response.status_code == 201
    stored = identity_document_model().objects.get().front_image
    with stored.file.open("rb") as handle:
        assert handle.read() == raw
    assert stored.sha256 == hashlib.sha256(raw).hexdigest()
    assert stored.size == len(raw)
    assert stored.media_type == mime
    serialized = response.json()["data"]
    forbidden = {"file", "path", "url", "sha256", "storage_key", "front_image"}
    assert forbidden.isdisjoint(serialized)
    assert str(stored.file.name) not in response.content.decode()


@pytest.mark.django_db
def test_owner_and_verification_agent_can_stream_original_image(api_client):
    owner, _ = create_patient()
    raw = image_bytes()
    created = submit(
        api_client,
        owner,
        national_card_payload(
            front_image=SimpleUploadedFile("front.png", raw, content_type="image/png")
        ),
    ).json()["data"]
    image_url = f"{COLLECTION}{created['uuid']}/images/front/"

    auth(api_client, owner)
    owner_response = api_client.get(image_url)
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    auth(api_client, agent)
    agent_response = api_client.get(image_url)

    assert owner_response.status_code == 200
    assert b"".join(owner_response.streaming_content) == raw
    assert agent_response.status_code == 200
    assert b"".join(agent_response.streaming_content) == raw
    assert owner_response["Content-Type"] == "image/png"
    assert "private-identity" not in str(owner_response.headers)


@pytest.mark.django_db
def test_agent_queue_is_exact_role_scoped_and_safe(api_client):
    owner, profile = create_patient()
    created = submit(api_client, owner).json()["data"]

    for role in ("PATIENT", "ADMIN"):
        denied = UserFactory(role=role, status="ACTIVE")
        auth(api_client, denied)
        assert_error(
            api_client.get(VERIFY_COLLECTION), 403, "verification_agent_required"
        )

    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    auth(api_client, agent)
    response = api_client.get(f"{VERIFY_COLLECTION}?status=PENDING")

    assert response.status_code == 200
    item = response.json()["data"]["results"][0]
    assert item["uuid"] == created["uuid"]
    assert item["patient"]["uuid"] == str(profile.uuid)
    assert set(item["patient"]) == {
        "uuid",
        "digital_id",
        "full_name",
        "date_of_birth",
        "sex",
        "nationality",
        "identity_status",
    }
    assert "family_number" not in item


@pytest.mark.django_db
def test_agent_queue_rejects_invalid_status_filter(api_client):
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    auth(api_client, agent)

    response = api_client.get(f"{VERIFY_COLLECTION}?status=UNKNOWN")

    assert_error(response, 400, "validation_error")


@pytest.mark.django_db
def test_only_agent_can_approve_or_reject_and_cannot_edit_identity(api_client):
    owner, _ = create_patient()
    document_uuid = submit(api_client, owner).json()["data"]["uuid"]

    auth(api_client, owner)
    approve_response = api_client.post(
        f"{VERIFY_COLLECTION}{document_uuid}/approve/",
        {"full_name": "Injected", "status": "REVOKED"},
        format="json",
    )
    reject_response = api_client.post(
        f"{VERIFY_COLLECTION}{document_uuid}/reject/",
        {"rejection_reason": "No match"},
        format="json",
    )

    assert_error(approve_response, 403, "verification_agent_required")
    assert_error(reject_response, 403, "verification_agent_required")


@pytest.mark.django_db
def test_agent_approval_records_reviewer_and_verifies_national_card(api_client):
    owner, profile = create_patient()
    document_uuid = submit(api_client, owner).json()["data"]["uuid"]
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")

    response = approve(api_client, agent, document_uuid)

    assert response.status_code == 200
    document = identity_document_model().objects.get()
    profile.refresh_from_db()
    assert document.verification_status == "VERIFIED"
    assert document.verified_by == agent
    assert document.verified_at is not None
    assert document.rejection_reason == ""
    assert profile.identity_status == "VERIFIED"
    assert (
        identity_event_model()
        .objects.filter(document=document, event_type="IDENTITY_VERIFIED", actor=agent)
        .exists()
    )


@pytest.mark.django_db
def test_approval_is_idempotent_for_same_agent(api_client):
    owner, _ = create_patient()
    document_uuid = submit(api_client, owner).json()["data"]["uuid"]
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")

    first = approve(api_client, agent, document_uuid)
    second = approve(api_client, agent, document_uuid)

    assert first.status_code == 200
    assert second.status_code == 200
    assert (
        identity_event_model().objects.filter(event_type="IDENTITY_VERIFIED").count()
        == 1
    )


@pytest.mark.django_db
def test_approval_rejects_payload_and_conflicting_transition(api_client):
    owner, _ = create_patient()
    document_uuid = submit(api_client, owner).json()["data"]["uuid"]
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    auth(api_client, agent)

    payload_response = api_client.post(
        f"{VERIFY_COLLECTION}{document_uuid}/approve/",
        {"verification_status": "VERIFIED"},
        format="json",
    )
    rejection = api_client.post(
        f"{VERIFY_COLLECTION}{document_uuid}/reject/",
        {"rejection_reason": "Image does not match."},
        format="json",
    )
    conflict = approve(api_client, agent, document_uuid)

    assert_error(payload_response, 400, "validation_error")
    assert rejection.status_code == 200
    assert_error(conflict, 409, "identity_transition_conflict")


@pytest.mark.django_db
def test_rejection_is_durable_and_profile_state_is_rejected(api_client):
    owner, profile = create_patient()
    document_uuid = submit(api_client, owner).json()["data"]["uuid"]
    document = identity_document_model().objects.get()
    front_name = document.front_image.file.name
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    auth(api_client, agent)

    response = api_client.post(
        f"{VERIFY_COLLECTION}{document_uuid}/reject/",
        {"rejection_reason": "Image is unreadable."},
        format="json",
    )

    assert response.status_code == 200
    document.refresh_from_db()
    profile.refresh_from_db()
    assert document.verification_status == "REJECTED"
    assert document.rejection_reason == "Image is unreadable."
    assert document.verified_by == agent
    assert document.verified_at is not None
    assert document.front_image.file.storage.exists(front_name)
    assert profile.identity_status == "REJECTED"
    assert (
        identity_event_model()
        .objects.filter(document=document, event_type="IDENTITY_REJECTED", actor=agent)
        .exists()
    )


@pytest.mark.django_db
@pytest.mark.parametrize("reason", ["", " ", "x" * 1001])
def test_rejection_reason_is_required_and_bounded(api_client, reason):
    owner, _ = create_patient()
    document_uuid = submit(api_client, owner).json()["data"]["uuid"]
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    auth(api_client, agent)

    response = api_client.post(
        f"{VERIFY_COLLECTION}{document_uuid}/reject/",
        {"rejection_reason": reason},
        format="json",
    )

    assert_error(response, 400, "validation_error")


@pytest.mark.django_db
def test_verified_passport_alone_does_not_verify_profile(api_client):
    owner, profile = create_patient()
    document_uuid = submit(api_client, owner, passport_payload()).json()["data"]["uuid"]
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")

    response = approve(api_client, agent, document_uuid)

    assert response.status_code == 200
    profile.refresh_from_db()
    assert profile.identity_status == "UNVERIFIED"


@pytest.mark.django_db
def test_pending_passport_and_national_card_state_is_deterministic(api_client):
    owner, profile = create_patient()
    submit(api_client, owner, passport_payload())
    profile.refresh_from_db()
    assert profile.identity_status == "UNVERIFIED"

    response = submit(api_client, owner)

    assert response.status_code == 201
    profile.refresh_from_db()
    assert profile.identity_status == "PENDING_VERIFICATION"


@pytest.mark.django_db
def test_duplicate_same_type_requires_explicit_replacement(api_client):
    owner, _ = create_patient()
    submit(api_client, owner)

    response = submit(
        api_client, owner, national_card_payload(document_number="CARD-002")
    )

    assert_error(response, 409, "identity_document_conflict")
    assert identity_document_model().objects.count() == 1


@pytest.mark.django_db
def test_replacement_approval_preserves_history_and_identifiers(api_client):
    owner, profile = create_patient()
    original_uuid = profile.uuid
    original_digital_id = profile.digital_id
    first_uuid = submit(api_client, owner).json()["data"]["uuid"]
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    approve(api_client, agent, first_uuid)
    auth(api_client, owner)

    replacement = api_client.post(
        f"{COLLECTION}{first_uuid}/replace/",
        national_card_payload(
            document_number="CARD-002",
            national_number="NAT-002",
            family_number="FAM-002",
        ),
        format="multipart",
    )

    assert replacement.status_code == 201
    first = identity_document_model().objects.get(uuid=first_uuid)
    second = identity_document_model().objects.get(
        uuid=replacement.json()["data"]["uuid"]
    )
    profile.refresh_from_db()
    assert first.status == "CURRENT"
    assert first.verification_status == "VERIFIED"
    assert second.status == "CURRENT"
    assert second.verification_status == "PENDING"
    assert second.replaces == first
    assert profile.identity_status == "VERIFIED"

    approved = approve(api_client, agent, second.uuid)

    assert approved.status_code == 200
    first.refresh_from_db()
    second.refresh_from_db()
    profile.refresh_from_db()
    assert first.status == "REPLACED"
    assert first.family_number == "FAM-001"
    assert second.status == "CURRENT"
    assert second.verification_status == "VERIFIED"
    assert second.family_number == "FAM-002"
    assert profile.uuid == original_uuid
    assert profile.digital_id == original_digital_id
    assert (
        identity_event_model()
        .objects.filter(document=first, event_type="IDENTITY_DOCUMENT_REPLACED")
        .exists()
    )


@pytest.mark.django_db
def test_rejected_replacement_keeps_original_current_and_profile_verified(api_client):
    owner, profile = create_patient()
    first_uuid = submit(api_client, owner).json()["data"]["uuid"]
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    approve(api_client, agent, first_uuid)
    auth(api_client, owner)
    replacement = api_client.post(
        f"{COLLECTION}{first_uuid}/replace/",
        national_card_payload(document_number="CARD-002"),
        format="multipart",
    ).json()["data"]
    auth(api_client, agent)

    response = api_client.post(
        f"{VERIFY_COLLECTION}{replacement['uuid']}/reject/",
        {"rejection_reason": "Mismatch."},
        format="json",
    )

    assert response.status_code == 200
    first = identity_document_model().objects.get(uuid=first_uuid)
    second = identity_document_model().objects.get(uuid=replacement["uuid"])
    profile.refresh_from_db()
    assert first.status == "CURRENT"
    assert first.verification_status == "VERIFIED"
    assert second.verification_status == "REJECTED"
    assert profile.identity_status == "VERIFIED"


@pytest.mark.django_db
def test_replacement_is_owner_scoped_and_requires_current_source(api_client):
    owner, _ = create_patient(email="owner@example.com")
    attacker, _ = create_patient(email="attacker@example.com")
    first_uuid = submit(api_client, owner).json()["data"]["uuid"]
    auth(api_client, attacker)

    idor = api_client.post(
        f"{COLLECTION}{first_uuid}/replace/",
        national_card_payload(document_number="STOLEN"),
        format="multipart",
    )

    assert_error(idor, 404, "not_found")


@pytest.mark.django_db
def test_database_prevents_two_verified_current_documents_same_type():
    from identities.models import IdentityDocument, IdentityFile

    _, profile = create_patient()
    identity_file = IdentityFile.objects.create(
        file=image_upload(),
        original_name="identity.png",
        media_type="image/png",
        size=len(image_bytes()),
        sha256=hashlib.sha256(image_bytes()).hexdigest(),
    )
    first = IdentityDocument.objects.create(
        patient=profile,
        document_type="PASSPORT",
        document_number="P1",
        issuing_country="IQ",
        front_image=identity_file,
        verification_status="VERIFIED",
        status="CURRENT",
    )
    assert first.pk

    with pytest.raises(IntegrityError):
        IdentityDocument.objects.create(
            patient=profile,
            document_type="PASSPORT",
            document_number="P2",
            issuing_country="IQ",
            front_image=identity_file,
            verification_status="VERIFIED",
            status="CURRENT",
        )


@pytest.mark.django_db
def test_approval_transaction_rolls_back_document_and_profile(api_client):
    owner, profile = create_patient()
    document_uuid = submit(api_client, owner).json()["data"]["uuid"]
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")

    with patch(
        "identities.services._record_event", side_effect=RuntimeError("audit failed")
    ):
        with pytest.raises(RuntimeError, match="audit failed"):
            approve(api_client, agent, document_uuid)

    document = identity_document_model().objects.get(uuid=document_uuid)
    profile.refresh_from_db()
    assert document.verification_status == "PENDING"
    assert document.verified_by is None
    assert profile.identity_status == "PENDING_VERIFICATION"


@pytest.mark.django_db
def test_storage_blobs_are_cleaned_when_document_transaction_fails(
    api_client, settings
):
    owner, _ = create_patient()

    with patch(
        "identities.services.IdentityDocument.objects.create",
        side_effect=IntegrityError("document failed"),
    ):
        with pytest.raises(IntegrityError, match="document failed"):
            submit(api_client, owner)

    root = Path(settings.IDENTITY_FILE_ROOT)
    assert not list(root.rglob("*")) if root.exists() else True
    assert not identity_file_model().objects.exists()


@pytest.mark.django_db
def test_identity_events_are_immutable_and_not_api_writable(api_client):
    from django.core.exceptions import ValidationError

    owner, _ = create_patient()
    document_uuid = submit(api_client, owner).json()["data"]["uuid"]
    event = identity_event_model().objects.get(event_type="IDENTITY_DOCUMENT_UPLOADED")
    event.metadata = {"family_number": "leak"}

    with pytest.raises(ValidationError, match="immutable"):
        event.save()
    with pytest.raises(ValidationError, match="immutable"):
        event.delete()

    auth(api_client, owner)
    assert api_client.post("/api/v1/identity-events/", {}).status_code == 404
    assert str(document_uuid) not in str(event.metadata)


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["patch", "put", "delete"])
def test_identity_detail_unsupported_methods_return_405(api_client, method):
    owner, _ = create_patient()
    document_uuid = submit(api_client, owner).json()["data"]["uuid"]
    auth(api_client, owner)

    response = getattr(api_client, method)(
        f"{COLLECTION}{document_uuid}/", {}, format="json"
    )

    assert_error(response, 405, "method_not_allowed")


@pytest.mark.django_db
def test_expired_document_date_is_rejected(api_client):
    owner, _ = create_patient()

    response = submit(
        api_client,
        owner,
        passport_payload(
            issue_date=str(date.today()),
            expiry_date=str(date.today() - timedelta(days=1)),
        ),
    )

    assert_error(response, 400, "validation_error")
    assert "expiry_date" in response.json()["error"]["details"]


@pytest.mark.django_db
def test_other_government_id_is_supported_without_custom_workflow(api_client):
    user, profile = create_patient()
    payload = {
        "document_type": "OTHER_GOVERNMENT_ID",
        "document_number": "GOV-9",
        "issuing_country": "jo",
        "front_image": image_upload(),
    }

    response = submit(api_client, user, payload)

    assert response.status_code == 201
    document = identity_document_model().objects.get()
    profile.refresh_from_db()
    assert document.issuing_country == "JO"
    assert profile.identity_status == "UNVERIFIED"


@pytest.mark.django_db
@pytest.mark.parametrize("missing", ["issue_date", "expiry_date"])
def test_passport_requires_issue_and_expiry_dates(api_client, missing):
    user, _ = create_patient()
    payload = passport_payload()
    payload.pop(missing)

    response = submit(api_client, user, payload)

    assert_error(response, 400, "validation_error")
    assert missing in response.json()["error"]["details"]


@pytest.mark.django_db
def test_missing_image_side_and_unrelated_staff_are_denied(api_client):
    owner, _ = create_patient()
    document_uuid = submit(api_client, owner, passport_payload()).json()["data"]["uuid"]
    back_url = f"{COLLECTION}{document_uuid}/images/back/"
    invalid_url = f"{COLLECTION}{document_uuid}/images/profile/"

    auth(api_client, owner)
    assert_error(api_client.get(back_url), 404, "not_found")
    assert_error(api_client.get(invalid_url), 404, "not_found")
    admin = UserFactory(role="ADMIN", status="ACTIVE")
    auth(api_client, admin)
    assert_error(api_client.get(back_url), 403, "patient_role_required")


@pytest.mark.django_db
def test_private_storage_has_no_url_and_random_name_contains_no_pii(api_client):
    user, profile = create_patient()
    response = submit(api_client, user)
    stored = identity_document_model().objects.get().front_image

    with pytest.raises(ValueError, match="public URLs"):
        assert stored.file.url
    assert profile.digital_id not in stored.file.name
    assert "CARD-001" not in stored.file.name
    assert "NAT-001" not in stored.file.name
    assert response.status_code == 201


@pytest.mark.django_db
def test_agent_detail_shows_required_identity_only_and_patient_is_denied(api_client):
    owner, _ = create_patient()
    document_uuid = submit(api_client, owner).json()["data"]["uuid"]
    verification_url = f"{VERIFY_COLLECTION}{document_uuid}/"

    auth(api_client, owner)
    assert_error(api_client.get(verification_url), 403, "verification_agent_required")
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    auth(api_client, agent)
    response = api_client.get(verification_url)

    assert response.status_code == 200
    detail = response.json()["data"]
    assert detail["family_number"] == "FAM-001"
    assert "patient" in detail
    assert {"file", "sha256", "storage_key", "path", "url"}.isdisjoint(detail)


@pytest.mark.django_db
def test_verification_unknown_uuid_returns_safe_not_found(api_client):
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    auth(api_client, agent)

    response = api_client.get(f"{VERIFY_COLLECTION}{uuid.uuid4()}/")

    assert_error(response, 404, "not_found")


@pytest.mark.django_db
def test_approval_by_different_agent_is_not_idempotent(api_client):
    owner, _ = create_patient()
    document_uuid = submit(api_client, owner).json()["data"]["uuid"]
    first_agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    second_agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    approve(api_client, first_agent, document_uuid)

    response = approve(api_client, second_agent, document_uuid)

    assert_error(response, 409, "identity_transition_conflict")


@pytest.mark.django_db
def test_repeated_rejection_is_a_conflict(api_client):
    owner, _ = create_patient()
    document_uuid = submit(api_client, owner).json()["data"]["uuid"]
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    auth(api_client, agent)
    url = f"{VERIFY_COLLECTION}{document_uuid}/reject/"

    assert (
        api_client.post(
            url, {"rejection_reason": "Mismatch."}, format="json"
        ).status_code
        == 200
    )
    response = api_client.post(url, {"rejection_reason": "Mismatch."}, format="json")

    assert_error(response, 409, "identity_transition_conflict")


@pytest.mark.django_db
def test_service_layer_rejects_non_agent_decisions(api_client):
    from identities.exceptions import VerificationAgentRequired
    from identities.services import approve_identity_document, reject_identity_document

    owner, _ = create_patient()
    submit(api_client, owner)
    document = identity_document_model().objects.get()

    with pytest.raises(VerificationAgentRequired):
        approve_identity_document(document=document, agent=owner)
    with pytest.raises(VerificationAgentRequired):
        reject_identity_document(document=document, agent=owner, reason="No.")


@pytest.mark.django_db
def test_rejected_card_can_be_resubmitted_and_returns_profile_to_pending(api_client):
    owner, profile = create_patient()
    first_uuid = submit(api_client, owner).json()["data"]["uuid"]
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    auth(api_client, agent)
    api_client.post(
        f"{VERIFY_COLLECTION}{first_uuid}/reject/",
        {"rejection_reason": "Unreadable."},
        format="json",
    )
    profile.refresh_from_db()
    assert profile.identity_status == "REJECTED"

    response = submit(
        api_client, owner, national_card_payload(document_number="CARD-002")
    )

    assert response.status_code == 201
    profile.refresh_from_db()
    assert profile.identity_status == "PENDING_VERIFICATION"


@pytest.mark.django_db
def test_second_pending_replacement_is_rejected(api_client):
    owner, _ = create_patient()
    first_uuid = submit(api_client, owner).json()["data"]["uuid"]
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    approve(api_client, agent, first_uuid)
    auth(api_client, owner)
    url = f"{COLLECTION}{first_uuid}/replace/"
    first = api_client.post(
        url, national_card_payload(document_number="CARD-002"), format="multipart"
    )

    second = api_client.post(
        url, national_card_payload(document_number="CARD-003"), format="multipart"
    )

    assert first.status_code == 201
    assert_error(second, 409, "identity_document_conflict")


@pytest.mark.django_db
def test_replacement_document_type_cannot_change(api_client):
    owner, _ = create_patient()
    first_uuid = submit(api_client, owner).json()["data"]["uuid"]
    auth(api_client, owner)

    response = api_client.post(
        f"{COLLECTION}{first_uuid}/replace/",
        passport_payload(),
        format="multipart",
    )

    assert_error(response, 409, "identity_transition_conflict")


@pytest.mark.django_db
def test_competing_replacement_approvals_cannot_create_two_current_cards(api_client):
    from identities.models import IdentityDocument

    owner, profile = create_patient()
    first_uuid = submit(api_client, owner).json()["data"]["uuid"]
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    approve(api_client, agent, first_uuid)
    auth(api_client, owner)
    second_data = api_client.post(
        f"{COLLECTION}{first_uuid}/replace/",
        national_card_payload(document_number="CARD-002"),
        format="multipart",
    ).json()["data"]
    second = IdentityDocument.objects.get(uuid=second_data["uuid"])
    competing = IdentityDocument.objects.create(
        patient=profile,
        document_type=second.document_type,
        document_number="CARD-003",
        national_number="NAT-003",
        family_number="FAM-003",
        issuing_country="IQ",
        front_image=second.front_image,
        back_image=second.back_image,
        replaces_id=first_uuid,
    )

    assert approve(api_client, agent, second.uuid).status_code == 200
    conflict = approve(api_client, agent, competing.uuid)

    assert_error(conflict, 409, "identity_transition_conflict")
    assert (
        IdentityDocument.objects.filter(
            patient=profile,
            document_type="UNIFIED_NATIONAL_CARD",
            verification_status="VERIFIED",
            status="CURRENT",
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_owner_can_retrieve_document_detail(api_client):
    owner, _ = create_patient()
    created = submit(api_client, owner).json()["data"]

    response = api_client.get(f"{COLLECTION}{created['uuid']}/")

    assert response.status_code == 200
    assert response.json()["data"] == created


@pytest.mark.django_db
def test_unfiltered_verification_queue_returns_all_statuses(api_client):
    owner, _ = create_patient()
    submit(api_client, owner, passport_payload())
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    auth(api_client, agent)

    response = api_client.get(VERIFY_COLLECTION)

    assert response.status_code == 200
    assert response.json()["data"]["count"] == 1


@pytest.mark.django_db
def test_approval_blocks_unlinked_competing_current_verified_document(api_client):
    from identities.models import IdentityDocument

    owner, profile = create_patient()
    first_uuid = submit(api_client, owner, passport_payload()).json()["data"]["uuid"]
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    approve(api_client, agent, first_uuid)
    first = IdentityDocument.objects.get(uuid=first_uuid)
    candidate = IdentityDocument.objects.create(
        patient=profile,
        document_type="PASSPORT",
        document_number="P2",
        issuing_country="IQ",
        front_image=first.front_image,
    )

    response = approve(api_client, agent, candidate.uuid)

    assert_error(response, 409, "identity_transition_conflict")


@pytest.mark.django_db
def test_orphan_blob_is_cleaned_if_identity_file_row_fails(api_client, settings):
    owner, _ = create_patient()

    with patch(
        "identities.services.IdentityFile.save",
        side_effect=IntegrityError("identity file failed"),
    ):
        with pytest.raises(IntegrityError, match="identity file failed"):
            submit(api_client, owner)

    root = Path(settings.IDENTITY_FILE_ROOT)
    assert not list(root.rglob("*")) if root.exists() else True


@pytest.mark.django_db
def test_valid_image_with_executable_suffix_is_rejected(api_client):
    owner, _ = create_patient()
    disguised = SimpleUploadedFile(
        "front.png",
        image_bytes("PNG") + b"MZ\x90\x00executable",
        content_type="image/png",
    )

    response = submit(api_client, owner, national_card_payload(front_image=disguised))

    assert_error(response, 400, "validation_error")
    assert "front_image" in response.json()["error"]["details"]


@pytest.mark.django_db
def test_pending_document_cannot_be_used_as_replacement_source(api_client):
    owner, _ = create_patient()
    pending_uuid = submit(api_client, owner).json()["data"]["uuid"]
    auth(api_client, owner)

    response = api_client.post(
        f"{COLLECTION}{pending_uuid}/replace/",
        national_card_payload(document_number="CARD-002"),
        format="multipart",
    )

    assert_error(response, 409, "identity_transition_conflict")


@pytest.mark.django_db
def test_national_card_defaults_iraq_but_passport_requires_country(api_client):
    owner, _ = create_patient()
    national_card = national_card_payload()
    national_card.pop("issuing_country")

    card_response = submit(api_client, owner, national_card)

    assert card_response.status_code == 201
    assert identity_document_model().objects.get().issuing_country == "IQ"

    second_owner, _ = create_patient(email="passport-owner@example.com")
    passport = passport_payload()
    passport.pop("issuing_country")
    passport_response = submit(api_client, second_owner, passport)

    assert_error(passport_response, 400, "validation_error")
    assert "issuing_country" in passport_response.json()["error"]["details"]


@pytest.mark.django_db
def test_future_issue_date_is_rejected(api_client):
    owner, _ = create_patient()

    response = submit(
        api_client,
        owner,
        passport_payload(issue_date=str(date.today() + timedelta(days=1))),
    )

    assert_error(response, 400, "validation_error")
    assert "issue_date" in response.json()["error"]["details"]


@pytest.mark.django_db
def test_verification_queue_rejects_patient_lookup_filter(api_client):
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    auth(api_client, agent)

    response = api_client.get(f"{VERIFY_COLLECTION}?patient={uuid.uuid4()}")

    assert_error(response, 400, "validation_error")
    assert "patient" in response.json()["error"]["details"]


@pytest.mark.django_db
def test_image_download_header_does_not_reveal_original_filename(api_client):
    owner, _ = create_patient()
    payload = national_card_payload(
        front_image=image_upload("Layla-Hassan-national-card.png")
    )
    document_uuid = submit(api_client, owner, payload).json()["data"]["uuid"]

    response = api_client.get(f"{COLLECTION}{document_uuid}/images/front/")

    assert response.status_code == 200
    assert "Layla-Hassan" not in response["Content-Disposition"]
