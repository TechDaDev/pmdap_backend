from datetime import date

import pytest

from claims.models import PatientAccountClaim
from guardians.models import GuardianRelationship
from identities.models import IdentityDocument
from tests.factories import UserFactory
from tests.test_account_claiming import (
    ACTIVATE,
    VERIFY,
    auth,
    payload,
    submit,
    verified_adult,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    settings.IDENTITY_FILE_MAX_BYTES = 1024 * 1024


def test_age_18_claim_activation_preserves_lifelong_identity(api_client):
    today = date.today()
    minor_birth_date = date(today.year - 17, today.month, today.day)
    adult_birth_date = date(today.year - 18, today.month, today.day)

    guardian = UserFactory(status="ACTIVE", email="guardian@example.com")
    verified_adult(digital_id="22345678901234567", owner=guardian)
    profile = verified_adult()
    type(profile).objects.filter(pk=profile.pk).update(date_of_birth=minor_birth_date)
    profile.refresh_from_db()
    relationship = GuardianRelationship.objects.create(
        guardian_user=guardian,
        minor_patient=profile,
        relationship=GuardianRelationship.Relationship.MOTHER,
        verification_status=GuardianRelationship.VerificationStatus.VERIFIED,
        active=True,
    )
    rejected_relationship = GuardianRelationship.objects.create(
        guardian_user=guardian,
        minor_patient=profile,
        relationship=GuardianRelationship.Relationship.FATHER,
        verification_status=GuardianRelationship.VerificationStatus.REJECTED,
        active=False,
        rejection_reason="Historical rejection.",
    )
    original_uuid = profile.uuid
    original_digital_id = profile.digital_id
    original_document_ids = set(
        IdentityDocument.objects.filter(patient=profile).values_list("uuid", flat=True)
    )

    auth(api_client, guardian)
    before_adulthood = api_client.get("/api/v1/minors/")
    assert before_adulthood.status_code == 200
    assert before_adulthood.json()["data"]["count"] == 1

    type(profile).objects.filter(pk=profile.pk).update(date_of_birth=adult_birth_date)
    profile.refresh_from_db()
    after_adulthood = api_client.get("/api/v1/minors/")
    assert after_adulthood.status_code == 200
    assert after_adulthood.json()["data"]["count"] == 0

    api_client.credentials()
    claim_response = submit(
        api_client,
        payload(date_of_birth=adult_birth_date.isoformat()),
    )
    assert claim_response.status_code == 202
    claim = PatientAccountClaim.objects.get()

    reviewer = UserFactory(
        role="IDENTITY_VERIFICATION_AGENT",
        status="ACTIVE",
        email="reviewer@example.com",
    )
    auth(api_client, reviewer)
    approval = api_client.post(f"{VERIFY}{claim.uuid}/approve/", {}, format="json")
    assert approval.status_code == 200
    activation_token = approval.json()["data"]["activation_token"]

    api_client.credentials()
    activation = api_client.post(
        ACTIVATE,
        {"token": activation_token, "new_password": "StrongPass456!"},
        format="json",
    )
    assert activation.status_code == 200
    login = api_client.post(
        "/api/v1/auth/login/",
        {"email": "claimant@example.com", "password": "StrongPass456!"},
        format="json",
    )
    assert login.status_code == 200
    api_client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {login.json()['data']['access']}"
    )
    assert (
        api_client.post(
            "/api/v1/auth/refresh/",
            {"refresh": login.json()["data"]["refresh"]},
            format="json",
        ).status_code
        == 200
    )
    assert api_client.get("/api/v1/auth/me/").status_code == 200
    patient_me = api_client.get("/api/v1/patients/me/")
    assert patient_me.status_code == 200

    profile.refresh_from_db()
    relationship.refresh_from_db()
    rejected_relationship.refresh_from_db()
    assert profile.uuid == original_uuid
    assert profile.digital_id == original_digital_id
    assert patient_me.json()["data"]["uuid"] == str(original_uuid)
    assert (
        set(
            IdentityDocument.objects.filter(patient=profile).values_list(
                "uuid", flat=True
            )
        )
        == original_document_ids
    )
    assert relationship.active is False
    assert relationship.ended_reason == "PATIENT_REACHED_ADULTHOOD"
    assert rejected_relationship.verification_status == "REJECTED"
    assert rejected_relationship.active is False
    assert rejected_relationship.rejection_reason == "Historical rejection."
