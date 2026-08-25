from datetime import date

import pytest
from django.test import override_settings

from accounts.models import User
from accounts.services import register_account
from audit.models import AuditLog
from claims.models import PatientAccountClaim
from claims.services.activation import activate_claimed_account
from claims.services.review import (
    approve_account_claim,
    transition_claim,
)
from claims.services.submission import submit_account_claim
from documents.date_services import confirm_document_date
from documents.services import (
    create_medical_document,
    soft_delete_medical_document,
    update_medical_document,
)
from guardians.models import GuardianRelationship
from identities.services import (
    reject_identity_document,
    submit_identity_document,
)
from processing.services import _read_verified_content
from tests.test_medical_documents_api import patient_user
from tests.test_minors_guardians import (
    create_minor,
    create_verified_guardian,
    image_upload,
    patient_model,
    relationship_model,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    settings.IDENTITY_FILE_MAX_BYTES = 1024 * 1024


def audit_actions():
    return set(AuditLog.objects.values_list("action", flat=True))


def test_account_creation_audit(api_client):
    user = register_account(
        email="audit-register@example.com",
        password="A-complex-password-2026!",
        patient={
            "full_name": "Audit Patient",
            "date_of_birth": date(1990, 1, 2),
            "sex": "UNSPECIFIED",
            "nationality": "IQ",
        },
    )
    entry = AuditLog.objects.get(action=AuditLog.Action.ACCOUNT_CREATED, actor=user)
    assert entry.patient.user == user
    assert entry.resource_type == "USER"
    assert entry.new_values == {"role": "PATIENT", "status": "ACTIVE"}


def test_identity_flow_audit():
    user, profile, agent = create_verified_guardian(email="audit-guardian@example.com")
    actions = audit_actions()
    assert AuditLog.Action.IDENTITY_DOCUMENT_UPLOADED in actions
    assert AuditLog.Action.IDENTITY_DOCUMENT_VERIFIED in actions
    assert AuditLog.Action.PATIENT_IDENTITY_STATUS_CHANGED in actions

    verified = AuditLog.objects.get(
        action=AuditLog.Action.IDENTITY_DOCUMENT_VERIFIED,
        patient=profile,
    )
    assert verified.actor == agent


def test_identity_reject_audit():
    user = User.objects.create_user(
        email="audit-reject-owner@example.com",
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    profile = patient_model().objects.create(
        user=user,
        digital_id="12345678901234577",
        full_name="Reject Owner",
        date_of_birth=date(1990, 1, 2),
        sex="UNSPECIFIED",
        nationality="IQ",
    )
    agent = User.objects.create_user(
        email="audit-reject-agent@example.com",
        password="A-complex-password-2026!",
        role=User.Role.IDENTITY_VERIFICATION_AGENT,
        status=User.Status.ACTIVE,
    )
    document = submit_identity_document(
        patient=profile,
        actor=user,
        validated_data={
            "document_type": "UNIFIED_NATIONAL_CARD",
            "document_number": "CARD-REJECT",
            "issuing_country": "IQ",
            "front_image": image_upload("reject-front.png"),
        },
    )
    reject_identity_document(document=document, agent=agent, reason="Unable to verify.")
    entry = AuditLog.objects.get(
        action=AuditLog.Action.IDENTITY_DOCUMENT_REJECTED, patient=profile
    )
    assert entry.actor == agent
    assert entry.new_values["verification_status"] == "REJECTED"


def test_guardian_flow_audit(api_client):
    guardian, guardian_profile, agent = create_verified_guardian(
        email="audit-g-minor@example.com"
    )
    create_minor(api_client, guardian)
    minor = patient_model().objects.exclude(pk=guardian_profile.pk).get()
    relationship = relationship_model().objects.get(minor_patient=minor)
    from tests.test_guardian_relationships import approve_minor_document

    approve_minor_document(minor, agent)
    actions = audit_actions()
    assert AuditLog.Action.MINOR_CREATED in actions
    assert AuditLog.Action.GUARDIAN_RELATIONSHIP_SUBMITTED in actions

    from guardians.services import approve_guardian_relationship

    approve_guardian_relationship(relationship=relationship, agent=agent)
    assert AuditLog.Action.GUARDIAN_RELATIONSHIP_VERIFIED in audit_actions()
    entry = AuditLog.objects.get(
        action=AuditLog.Action.GUARDIAN_RELATIONSHIP_VERIFIED,
        patient=minor,
    )
    assert entry.actor == agent
    assert entry.resource_uuid == relationship.uuid


def test_guardian_reject_audit(api_client):
    guardian, guardian_profile, agent = create_verified_guardian(
        email="audit-g-reject@example.com"
    )
    create_minor(api_client, guardian)
    minor = patient_model().objects.exclude(pk=guardian_profile.pk).get()
    relationship = relationship_model().objects.get(minor_patient=minor)
    from guardians.services import reject_guardian_relationship

    reject_guardian_relationship(
        relationship=relationship, agent=agent, reason="Rejected."
    )
    entry = AuditLog.objects.get(
        action=AuditLog.Action.GUARDIAN_RELATIONSHIP_REJECTED, patient=minor
    )
    assert entry.actor == agent


def test_document_flow_audit(api_client, tmp_path):
    user, patient = patient_user()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        from tests.test_medical_documents_api import upload

        document = create_medical_document(
            patient=patient,
            actor=user,
            upload=upload(),
            metadata={"document_type": "OTHER", "title": "CBC"},
        )
    assert AuditLog.objects.filter(
        action=AuditLog.Action.DOCUMENT_UPLOADED,
        patient=patient,
        resource_uuid=document.uuid,
        actor=user,
    ).exists()

    update_medical_document(
        document=document,
        actor=user,
        metadata={"document_type": "LABORATORY"},
    )
    assert AuditLog.objects.filter(
        action=AuditLog.Action.DOCUMENT_TYPE_CHANGED,
        patient=patient,
        resource_uuid=document.uuid,
        previous_values={"document_type": "OTHER"},
        new_values={"document_type": "LABORATORY"},
    ).exists()

    update_medical_document(
        document=document, actor=user, metadata={"title": "CBC Updated"}
    )
    assert AuditLog.objects.filter(
        action=AuditLog.Action.DOCUMENT_METADATA_UPDATED,
        patient=patient,
        resource_uuid=document.uuid,
    ).exists()

    soft_delete_medical_document(document=document, actor=user)
    assert AuditLog.objects.filter(
        action=AuditLog.Action.DOCUMENT_DELETED,
        patient=patient,
        resource_uuid=document.uuid,
        actor=user,
    ).exists()


def test_date_authority_audit(api_client):
    from processing.date_services import process_date_candidates
    from tests.test_date_processing import prepared_document

    document = prepared_document("Report Date: 14/03/2026\n")
    patient = document.patient
    actor = document.patient.user
    process_date_candidates(str(document.uuid))
    candidate = document.date_candidates.get(is_current=True, is_suggested=True)
    confirm_document_date(document=document, actor=actor, candidate_id=candidate.uuid)
    assert AuditLog.objects.filter(
        action=AuditLog.Action.DATE_CONFIRMED,
        patient=patient,
        resource_uuid=document.uuid,
        new_values={"document_date": "2026-03-14"},
    ).exists()

    confirm_document_date(document=document, actor=actor, manual_date=date(2026, 4, 2))
    assert AuditLog.objects.filter(
        action=AuditLog.Action.DATE_CORRECTED,
        patient=patient,
        resource_uuid=document.uuid,
        previous_values={"document_date": "2026-03-14"},
        new_values={"document_date": "2026-04-02"},
    ).exists()


def test_date_confirm_replay_does_not_duplicate_audit(api_client):
    from processing.date_services import process_date_candidates
    from tests.test_date_processing import prepared_document

    document = prepared_document("Report Date: 14/03/2026\n")
    actor = document.patient.user
    process_date_candidates(str(document.uuid))
    candidate = document.date_candidates.get(is_current=True, is_suggested=True)
    confirm_document_date(document=document, actor=actor, candidate_id=candidate.uuid)
    count = AuditLog.objects.filter(
        action=AuditLog.Action.DATE_CONFIRMED,
        resource_uuid=document.uuid,
    ).count()
    confirm_document_date(document=document, actor=actor, candidate_id=candidate.uuid)
    assert (
        AuditLog.objects.filter(
            action=AuditLog.Action.DATE_CONFIRMED,
            resource_uuid=document.uuid,
        ).count()
        == count
    )


def test_claim_flow_audit(api_client):
    from tests.test_account_claiming import image_upload as claim_img
    from tests.test_account_claiming import verified_adult

    profile = verified_adult()
    relationship = GuardianRelationship.objects.create(
        guardian_user=User.objects.create_user(
            email="audit-guardian-claim@example.com",
            password="A-complex-password-2026!",
            status=User.Status.ACTIVE,
        ),
        minor_patient=profile,
        relationship=GuardianRelationship.Relationship.FATHER,
        verification_status=GuardianRelationship.VerificationStatus.VERIFIED,
        active=True,
    )
    agent = User.objects.create_user(
        email="audit-claim-agent@example.com",
        password="A-complex-password-2026!",
        role=User.Role.IDENTITY_VERIFICATION_AGENT,
        status=User.Status.ACTIVE,
    )
    receipt = submit_account_claim(
        {
            "digital_id": "12345678901234567",
            "email": "audit-claimant@example.com",
            "phone": "+9647700000000",
            "full_name": "Layla Hassan",
            "date_of_birth": date(1990, 1, 2),
            "identity_document_number": "CARD-001",
            "front_image": claim_img("claim-front.png"),
            "back_image": claim_img("claim-back.png"),
        }
    )
    claim = PatientAccountClaim.objects.get(uuid=receipt.claim_id)
    assert AuditLog.objects.filter(
        action=AuditLog.Action.CLAIM_SUBMITTED,
        patient=profile,
        resource_uuid=claim.uuid,
    ).exists()

    approved = approve_account_claim(claim=claim, agent=agent)
    assert AuditLog.objects.filter(
        action=AuditLog.Action.CLAIM_APPROVED, patient=profile
    ).exists()
    assert AuditLog.objects.filter(
        action=AuditLog.Action.PATIENT_ACCOUNT_LINKED, patient=profile
    ).exists()
    assert AuditLog.objects.filter(
        action=AuditLog.Action.ACCOUNT_ACTIVATION_CREATED, patient=profile
    ).exists()
    assert AuditLog.objects.filter(
        action=AuditLog.Action.GUARDIAN_ACCESS_EXPIRED,
        patient=profile,
        resource_uuid=relationship.uuid,
    ).exists()

    activate_claimed_account(
        token=approved.activation_token, new_password="A-complex-password-2026!"
    )
    assert AuditLog.objects.filter(
        action=AuditLog.Action.ACCOUNT_ACTIVATED, patient=profile
    ).exists()


def test_claim_reject_audit(api_client):
    from tests.test_account_claiming import image_upload as claim_img
    from tests.test_account_claiming import verified_adult

    profile = verified_adult()
    agent = User.objects.create_user(
        email="audit-claim-reject@example.com",
        password="A-complex-password-2026!",
        role=User.Role.IDENTITY_VERIFICATION_AGENT,
        status=User.Status.ACTIVE,
    )
    receipt = submit_account_claim(
        {
            "digital_id": "12345678901234567",
            "email": "audit-reject-claim@example.com",
            "phone": "+9647700000000",
            "full_name": "Layla Hassan",
            "date_of_birth": date(1990, 1, 2),
            "identity_document_number": "CARD-001",
            "front_image": claim_img("r-front.png"),
            "back_image": claim_img("r-back.png"),
        }
    )
    claim = PatientAccountClaim.objects.get(uuid=receipt.claim_id)
    transition_claim(
        claim=claim, agent=agent, status=PatientAccountClaim.Status.REJECTED, reason="N"
    )
    assert AuditLog.objects.filter(
        action=AuditLog.Action.CLAIM_REJECTED, patient=profile
    ).exists()


def test_processing_failure_audit(api_client, tmp_path):
    from tests.test_medical_documents_api import upload as doc_upload

    user, patient = patient_user()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        document = create_medical_document(
            patient=patient,
            actor=user,
            upload=doc_upload(),
            metadata={"document_type": "LABORATORY"},
        )
    stored = document.stored_file
    stored.integrity_status = "VALID"
    stored.save(update_fields=("integrity_status", "updated_at"))
    # Corrupt the blob bytes in the isolated test storage.
    with stored.file.open("wb") as handle:
        handle.write(b"tampered-bytes")
    result, code = _read_verified_content(stored)
    assert result is None
    assert code == "medical_file_integrity_mismatch"
    assert AuditLog.objects.filter(
        action=AuditLog.Action.INTEGRITY_FAILURE,
        patient=patient,
        resource_uuid=document.uuid,
    ).exists()


def test_audit_transactional_rollback(api_client, tmp_path):
    from documents.services import update_medical_document
    from tests.archive_helpers import make_facility
    from tests.test_medical_documents_api import upload as doc_upload

    user, patient = patient_user()
    inactive = make_facility(active=False)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        document = create_medical_document(
            patient=patient,
            actor=user,
            upload=doc_upload(),
            metadata={"document_type": "OTHER"},
        )
    baseline = AuditLog.objects.count()
    with pytest.raises(Exception, match="inactive"):
        update_medical_document(
            document=document,
            actor=user,
            metadata={
                "document_type": "LABORATORY",
                "healthcare_facility_id": str(inactive.uuid),
            },
        )
    # The failing inactive-facility lookup rolls back the whole transaction,
    # so the document type change and its audit are absent.
    document.refresh_from_db()
    assert document.document_type == "OTHER"
    assert AuditLog.objects.count() == baseline
