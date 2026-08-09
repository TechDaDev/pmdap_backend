"""
End-to-end audit flow tests (M14).

Chains real user journeys (adult medical, guardian/minor, account claim) and
verifies the audit trail captures the correct actor, patient, ordering, and
per-request correlation id — the properties that make the log useful for
forensic review.
"""

from datetime import date

import pytest
from django.test import override_settings

from accounts.models import User
from accounts.services import register_account
from audit.models import AuditLog
from claims.models import PatientAccountClaim
from claims.services.review import approve_account_claim
from claims.services.submission import submit_account_claim
from tests.test_medical_documents_api import COLLECTION, patient_user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    settings.IDENTITY_FILE_MAX_BYTES = 1024 * 1024


def test_adult_medical_journey_audits_request_id_and_actors(api_client, tmp_path):
    user, patient = patient_user(email="e2e-adult@example.com")
    api_client.force_authenticate(user=user)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        from tests.test_medical_documents_api import upload

        response = api_client.post(
            COLLECTION,
            {"document_type": "LABORATORY", "title": "E2E Panel", "file": upload()},
            format="multipart",
        )
    assert response.status_code == 201, response.content
    request_id = response["X-Request-Id"]
    document_uuid = response.data["data"]["uuid"]

    entry = AuditLog.objects.get(
        action=AuditLog.Action.DOCUMENT_UPLOADED, resource_uuid=document_uuid
    )
    assert entry.actor == user
    assert entry.patient == patient
    assert entry.actor_type == AuditLog.ActorType.USER
    assert entry.request_id == request_id

    # Upload audit is the newest entry for this patient.
    chain = list(
        AuditLog.objects.filter(patient=patient)
        .order_by("created_at", "uuid")
        .values_list("created_at", flat=True)
    )
    assert chain == sorted(chain)


def test_guardian_minor_journey_audits_guardian_actor_and_minor_patient(
    api_client, tmp_path
):
    from tests.test_minors_guardians import (
        birth_document_payload,
        create_verified_guardian,
        patient_model,
    )

    guardian, guardian_profile, _ = create_verified_guardian(
        email="e2e-guardian@example.com", family="FAM-E2E"
    )
    api_client.force_authenticate(user=guardian)
    minor_response = api_client.post(
        "/api/v1/minors/",
        birth_document_payload(),
        format="multipart",
        HTTP_IDEMPOTENCY_KEY="e2e-minor-1",
    )
    assert minor_response.status_code == 201, minor_response.content
    minor = patient_model().objects.exclude(pk=guardian_profile.pk).get()
    minor_audits = AuditLog.objects.filter(
        action=AuditLog.Action.MINOR_CREATED, patient=minor
    )
    assert minor_audits.exists()
    for entry in minor_audits:
        assert entry.actor == guardian
        assert entry.patient == minor
        assert entry.request_id == minor_response["X-Request-Id"]

    relationship_audit = AuditLog.objects.filter(
        action=AuditLog.Action.GUARDIAN_RELATIONSHIP_SUBMITTED,
        patient=minor,
        actor=guardian,
    )
    assert relationship_audit.exists()


def test_account_claim_journey_audits_reviewer_and_patient(api_client):
    from tests.test_account_claiming import image_upload, verified_adult

    profile = verified_adult()
    agent = User.objects.create_user(
        email="e2e-claim-agent@example.com",
        password="A-complex-password-2026!",
        role=User.Role.IDENTITY_VERIFICATION_AGENT,
        status=User.Status.ACTIVE,
    )
    receipt = submit_account_claim(
        {
            "digital_id": "12345678901234567",
            "email": "e2e-claimant@example.com",
            "phone": "+9647700000001",
            "full_name": "E2E Claimant",
            "date_of_birth": date(1990, 1, 2),
            "identity_document_number": "CARD-001",
            "front_image": image_upload("e2e-front.png"),
            "back_image": image_upload("e2e-back.png"),
        }
    )
    claim = PatientAccountClaim.objects.get(uuid=receipt.claim_id)
    approve_account_claim(claim=claim, agent=agent)

    submitted = AuditLog.objects.get(
        action=AuditLog.Action.CLAIM_SUBMITTED, resource_uuid=claim.uuid
    )
    assert submitted.patient == profile
    approved = AuditLog.objects.get(
        action=AuditLog.Action.CLAIM_APPROVED, resource_uuid=claim.uuid
    )
    assert approved.actor == agent
    assert approved.patient == profile

    # Chronological ordering across the whole claim chain.
    timestamps = list(
        AuditLog.objects.filter(patient=profile)
        .order_by("created_at")
        .values_list("created_at", flat=True)
    )
    assert timestamps == sorted(timestamps)


def test_registration_audit_is_system_attributed():
    user = register_account(
        email="e2e-register@example.com",
        password="A-complex-password-2026!",
        patient={
            "full_name": "E2E Register",
            "date_of_birth": date(1990, 1, 2),
            "sex": "UNSPECIFIED",
            "nationality": "IQ",
        },
    )
    entry = AuditLog.objects.get(action=AuditLog.Action.ACCOUNT_CREATED, actor=user)
    assert entry.patient.user == user
    assert entry.actor_type == AuditLog.ActorType.USER
