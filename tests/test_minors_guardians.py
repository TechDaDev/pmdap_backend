import io
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.apps import apps
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework_simplejwt.tokens import RefreshToken

from tests.factories import UserFactory

MINORS = "/api/v1/minors/"
VERIFY_RELATIONSHIPS = "/api/v1/verification/guardian-relationships/"
PROFILE_INPUT = {
    "full_name": "Noor Hassan",
    "date_of_birth": "2015-05-10",
    "sex": "FEMALE",
    "nationality": "IQ",
    "blood_group": "O+",
}
GUARDIAN_PROFILE_INPUT = {
    "full_name": "Layla Hassan",
    "date_of_birth": "1988-01-15",
    "sex": "FEMALE",
    "nationality": "IQ",
    "blood_group": "A+",
}


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    settings.IDENTITY_FILE_MAX_BYTES = 1024 * 1024


def patient_model():
    return apps.get_model("patients", "PatientProfile")


def document_model():
    return apps.get_model("identities", "IdentityDocument")


def relationship_model():
    return apps.get_model("guardians", "GuardianRelationship")


def evidence_model():
    return apps.get_model("guardians", "GuardianEvidence")


def relationship_event_model():
    return apps.get_model("guardians", "GuardianRelationshipEvent")


def creation_request_model():
    return apps.get_model("guardians", "MinorCreationRequest")


def auth(api_client, user):
    access = str(RefreshToken.for_user(user).access_token)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")


def image_bytes(image_format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (8, 8), color=(65, 100, 140)).save(output, format=image_format)
    return output.getvalue()


def image_upload(name="identity.png", image_format="PNG"):
    media_type = "image/png" if image_format == "PNG" else "image/jpeg"
    return SimpleUploadedFile(name, image_bytes(image_format), content_type=media_type)


def create_profile(user, **overrides):
    from patients.services import create_patient_profile

    return create_patient_profile(user=user, **{**GUARDIAN_PROFILE_INPUT, **overrides})


def create_verified_guardian(*, email="guardian@example.com", family="FAM-100"):
    from identities.services import approve_identity_document, submit_identity_document

    user = UserFactory(email=email, status="ACTIVE")
    profile = create_profile(user)
    agent = UserFactory(
        email=f"agent-{email}",
        role="IDENTITY_VERIFICATION_AGENT",
        status="ACTIVE",
    )
    document = submit_identity_document(
        patient=profile,
        actor=user,
        validated_data={
            "document_type": "UNIFIED_NATIONAL_CARD",
            "document_number": f"CARD-{email}",
            "national_number": f"NAT-{email}",
            "family_number": family,
            "issuing_country": "IQ",
            "issue_date": date(2022, 1, 1),
            "expiry_date": date(2032, 1, 1),
            "front_image": image_upload("guardian-front.png"),
            "back_image": image_upload("guardian-back.png"),
        },
    )
    approve_identity_document(document=document, agent=agent)
    profile.refresh_from_db()
    return user, profile, agent


def birth_document_payload(**overrides):
    payload = {
        **PROFILE_INPUT,
        "relationship": "MOTHER",
        "document_type": "BIRTH_DOCUMENT",
        "document_number": "BIRTH-001",
        "issuing_country": "IQ",
        "issue_date": "2015-05-11",
        "front_image": image_upload("birth.png"),
    }
    payload.update(overrides)
    return payload


def national_card_payload(**overrides):
    payload = {
        **PROFILE_INPUT,
        "relationship": "MOTHER",
        "document_type": "UNIFIED_NATIONAL_CARD",
        "document_number": "CARD-CHILD-1",
        "national_number": "NAT-CHILD-1",
        "family_number": "FAM-100",
        "issuing_country": "IQ",
        "issue_date": "2022-01-01",
        "expiry_date": "2032-01-01",
        "front_image": image_upload("child-front.png"),
        "back_image": image_upload("child-back.png"),
    }
    payload.update(overrides)
    return payload


def legal_guardian_payload(**overrides):
    payload = birth_document_payload(
        relationship="LEGAL_GUARDIAN",
        evidence_type="COURT_DOCUMENT",
        evidence_file=image_upload("court-order.png"),
    )
    payload.update(overrides)
    return payload


def create_minor(api_client, guardian, payload=None, key="minor-create-1"):
    auth(api_client, guardian)
    return api_client.post(
        MINORS,
        payload or birth_document_payload(),
        format="multipart",
        HTTP_IDEMPOTENCY_KEY=key,
    )


def assert_error(response, status_code, code=None):
    assert response.status_code == status_code
    assert set(response.json()) == {"error"}
    error = response.json()["error"]
    assert set(error) == {"code", "message", "details"}
    if code:
        assert error["code"] == code


@pytest.mark.django_db
def test_verified_adult_patient_creates_independent_minor(api_client):
    guardian, guardian_profile, _ = create_verified_guardian()

    response = create_minor(api_client, guardian)

    assert response.status_code == 201
    minor = patient_model().objects.exclude(pk=guardian_profile.pk).get()
    document = document_model().objects.get(patient=minor)
    relationship = relationship_model().objects.get(minor_patient=minor)
    assert minor.user is None
    assert minor.uuid != guardian_profile.uuid
    assert minor.digital_id != guardian_profile.digital_id
    assert minor.full_name == "Noor Hassan"
    assert minor.identity_status == "PENDING_VERIFICATION"
    assert document.document_type == "BIRTH_DOCUMENT"
    assert document.patient == minor
    assert relationship.guardian_user == guardian
    assert relationship.relationship == "MOTHER"
    assert relationship.verification_status == "PENDING"
    assert relationship.active is False
    assert relationship.family_number_result == "UNAVAILABLE"
    assert creation_request_model().objects.get().minor_patient == minor
    assert set(
        relationship_event_model().objects.values_list("event_type", flat=True)
    ) >= {
        "MINOR_CREATED",
        "GUARDIAN_RELATIONSHIP_SUBMITTED",
        "MINOR_IDENTITY_DOCUMENT_SUBMITTED",
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("identity_status", "code"),
    [
        ("UNVERIFIED", "guardian_not_verified"),
        ("PENDING_VERIFICATION", "guardian_not_verified"),
        ("REJECTED", "guardian_not_verified"),
    ],
)
def test_non_verified_guardian_identity_is_denied(api_client, identity_status, code):
    guardian, profile, _ = create_verified_guardian()
    patient_model().objects.filter(pk=profile.pk).update(
        identity_status=identity_status
    )

    response = create_minor(api_client, guardian)

    assert_error(response, 403, code)
    assert patient_model().objects.count() == 1


@pytest.mark.django_db
def test_guardian_requires_current_verified_national_card(api_client):
    guardian, profile, _ = create_verified_guardian()
    document_model().objects.filter(patient=profile).update(status="REVOKED")

    response = create_minor(api_client, guardian)

    assert_error(response, 403, "guardian_not_verified")


@pytest.mark.django_db
@pytest.mark.parametrize("account_change", ["SUSPENDED", "inactive"])
def test_unavailable_account_cannot_create_minor(api_client, account_change):
    guardian, _, _ = create_verified_guardian()
    auth(api_client, guardian)
    if account_change == "inactive":
        guardian.is_active = False
    else:
        guardian.status = account_change
    guardian.save()

    response = api_client.post(
        MINORS,
        birth_document_payload(),
        format="multipart",
        HTTP_IDEMPOTENCY_KEY="unavailable-account",
    )

    assert_error(response, 401, "account_unavailable")


@pytest.mark.django_db
def test_verification_agent_role_cannot_create_minor(api_client):
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    auth(api_client, agent)

    response = api_client.post(
        MINORS,
        birth_document_payload(),
        format="multipart",
        HTTP_IDEMPOTENCY_KEY="agent-attempt",
    )

    assert_error(response, 403, "guardian_not_verified")


@pytest.mark.django_db
def test_guardian_profile_must_be_adult(api_client):
    guardian = UserFactory(status="ACTIVE")
    profile = patient_model().objects.create(
        user=guardian,
        digital_id="PT-AAAA-BBBB-CCCC",
        **{
            **GUARDIAN_PROFILE_INPUT,
            "date_of_birth": date.today().replace(year=date.today().year - 12),
            "identity_status": "VERIFIED",
        },
    )
    assert profile.is_minor
    auth(api_client, guardian)

    response = api_client.post(
        MINORS,
        birth_document_payload(),
        format="multipart",
        HTTP_IDEMPOTENCY_KEY="minor-guardian-attempt",
    )

    assert_error(response, 403, "guardian_not_verified")


@pytest.mark.django_db
def test_minor_creation_requires_idempotency_key(api_client):
    guardian, _, _ = create_verified_guardian()
    auth(api_client, guardian)

    response = api_client.post(MINORS, birth_document_payload(), format="multipart")

    assert_error(response, 400, "idempotency_key_required")


@pytest.mark.django_db
@pytest.mark.parametrize("key", ["", " ", "x" * 129])
def test_minor_creation_rejects_invalid_idempotency_key(api_client, key):
    guardian, _, _ = create_verified_guardian()

    response = create_minor(api_client, guardian, key=key)

    assert_error(response, 400, "invalid_idempotency_key")


@pytest.mark.django_db
def test_same_idempotency_key_and_request_replays_one_minor(api_client):
    guardian, guardian_profile, _ = create_verified_guardian()

    first = create_minor(api_client, guardian, key="stable-retry")
    second = create_minor(api_client, guardian, key="stable-retry")

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json() == second.json()
    assert patient_model().objects.exclude(pk=guardian_profile.pk).count() == 1
    assert relationship_model().objects.count() == 1
    assert document_model().objects.exclude(patient=guardian_profile).count() == 1
    assert creation_request_model().objects.count() == 1


@pytest.mark.django_db
def test_same_idempotency_key_with_changed_request_conflicts(api_client):
    guardian, _, _ = create_verified_guardian()
    first = create_minor(api_client, guardian, key="conflicting-retry")

    second = create_minor(
        api_client,
        guardian,
        birth_document_payload(full_name="Different Child"),
        key="conflicting-retry",
    )

    assert first.status_code == 201
    assert_error(second, 409, "idempotency_conflict")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("date_of_birth", "accepted"),
    [
        (str(date.today() + timedelta(days=1)), False),
        (str(date.today().replace(year=date.today().year - 18)), False),
        (
            str(date.today().replace(year=date.today().year - 18) + timedelta(days=1)),
            True,
        ),
    ],
)
def test_minor_age_boundaries(api_client, date_of_birth, accepted):
    guardian, guardian_profile, _ = create_verified_guardian()

    response = create_minor(
        api_client,
        guardian,
        birth_document_payload(date_of_birth=date_of_birth),
        key=f"age-{date_of_birth}",
    )

    assert response.status_code == (201 if accepted else 400)
    if not accepted:
        assert_error(response, 400, "patient_not_minor")
        assert patient_model().objects.exclude(pk=guardian_profile.pk).count() == 0


@pytest.mark.django_db
def test_malformed_minor_dob_rejected(api_client):
    guardian, _profile, _ = create_verified_guardian()

    response = create_minor(
        api_client,
        guardian,
        birth_document_payload(date_of_birth="10-05-2015"),
    )

    assert_error(response, 400, "validation_error")


@pytest.mark.django_db
@pytest.mark.parametrize("document_type", ["PASSPORT", "OTHER_GOVERNMENT_ID"])
def test_minor_creation_rejects_non_primary_document_type(api_client, document_type):
    guardian, _, _ = create_verified_guardian()

    response = create_minor(
        api_client,
        guardian,
        birth_document_payload(document_type=document_type),
    )

    assert_error(response, 400, "validation_error")
    assert "document_type" in response.json()["error"]["details"]


@pytest.mark.django_db
def test_legal_guardian_requires_official_evidence(api_client):
    guardian, _, _ = create_verified_guardian()

    response = create_minor(
        api_client,
        guardian,
        birth_document_payload(relationship="LEGAL_GUARDIAN"),
    )

    assert_error(response, 400, "relationship_evidence_required")


@pytest.mark.django_db
def test_legal_guardian_evidence_uses_private_identity_file(api_client):
    guardian, _, _ = create_verified_guardian()

    response = create_minor(api_client, guardian, legal_guardian_payload())

    assert response.status_code == 201
    evidence = evidence_model().objects.get()
    assert evidence.evidence_type == "COURT_DOCUMENT"
    assert evidence.file.media_type == "image/png"
    assert "file" not in response.json()["data"]
    with pytest.raises(ValueError, match="public URLs"):
        assert evidence.file.file.url


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("child_family", "expected"),
    [
        ("FAM-100", "MATCH"),
        ("FAM-999", "MISMATCH"),
        ("", "UNAVAILABLE"),
    ],
)
def test_family_number_is_only_a_relationship_signal(
    api_client, child_family, expected
):
    guardian, guardian_profile, _ = create_verified_guardian(family="FAM-100")
    guardian_uuid = guardian_profile.uuid
    guardian_digital_id = guardian_profile.digital_id

    response = create_minor(
        api_client,
        guardian,
        national_card_payload(family_number=child_family),
        key=f"family-{expected}",
    )

    assert response.status_code == 201
    relationship = relationship_model().objects.get()
    guardian_profile.refresh_from_db()
    assert relationship.family_number_result == expected
    assert relationship.verification_status == "PENDING"
    assert guardian_profile.uuid == guardian_uuid
    assert guardian_profile.digital_id == guardian_digital_id
    expected_event = {
        "MATCH": "FAMILY_NUMBER_MATCHED",
        "MISMATCH": "FAMILY_NUMBER_MISMATCHED",
    }.get(expected)
    if expected_event:
        assert (
            relationship_event_model()
            .objects.filter(relationship=relationship, event_type=expected_event)
            .exists()
        )


@pytest.mark.django_db
def test_minor_creation_is_atomic_and_cleans_new_storage(api_client, settings):
    guardian, guardian_profile, _ = create_verified_guardian()
    root = Path(settings.IDENTITY_FILE_ROOT)
    before = {path for path in root.rglob("*") if path.is_file()}

    with patch(
        "guardians.services.GuardianRelationship.objects.create",
        side_effect=RuntimeError("relationship failed"),
    ):
        with pytest.raises(RuntimeError, match="relationship failed"):
            create_minor(api_client, guardian, national_card_payload())

    after = {path for path in root.rglob("*") if path.is_file()}
    assert before == after
    assert patient_model().objects.exclude(pk=guardian_profile.pk).count() == 0
    assert document_model().objects.exclude(patient=guardian_profile).count() == 0
    assert not relationship_model().objects.exists()
    assert not creation_request_model().objects.exists()


@pytest.mark.django_db
def test_patient_cannot_mass_assign_minor_or_relationship_state(api_client):
    guardian, _, _ = create_verified_guardian()
    protected = {
        "user": str(guardian.uuid),
        "digital_id": "PT-ZZZZ-ZZZZ-ZZZZ",
        "identity_status": "VERIFIED",
        "verification_status": "VERIFIED",
        "active": True,
        "verified_by": str(guardian.uuid),
        "family_number_result": "MATCH",
    }

    for index, (field, value) in enumerate(protected.items()):
        response = create_minor(
            api_client,
            guardian,
            birth_document_payload(**{field: value}),
            key=f"mass-assignment-{index}",
        )
        assert_error(response, 400, "validation_error")
        assert field in response.json()["error"]["details"]


@pytest.mark.django_db
def test_minor_creation_requires_authentication(api_client):
    response = api_client.post(
        MINORS,
        birth_document_payload(),
        format="multipart",
        HTTP_IDEMPOTENCY_KEY="anonymous",
    )

    assert_error(response, 401, "not_authenticated")
