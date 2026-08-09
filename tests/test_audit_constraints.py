"""
PostgreSQL-only constraint smoke tests (M14 #30).

Exercises the most security-critical DB-level constraints that protect the
audit trail and medical records: identity uniqueness, claim cardinality,
guardian relationship integrity, and immutable audit storage.
"""

from datetime import date

import pytest
from django.db import IntegrityError, connection, transaction

from accounts.models import User
from audit.models import AuditLog
from claims.models import PatientAccountClaim
from guardians.models import GuardianRelationship
from identities.models import IdentityDocument
from patients.models import PatientProfile
from tests.factories import UserFactory
from tests.test_account_claiming import verified_adult
from tests.test_minors_guardians import (
    create_verified_guardian,
    image_upload,
    patient_model,
)

pytestmark = [pytest.mark.postgresql, pytest.mark.django_db]


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    settings.IDENTITY_FILE_MAX_BYTES = 1024 * 1024


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only constraint smoke test")


def expect_integrity_error(operation):
    with transaction.atomic():
        with pytest.raises(IntegrityError):
            operation()


def test_user_email_is_unique_case_insensitive():
    require_postgresql()
    User.objects.create_user(
        email="constraint@example.com",
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )

    def duplicate():
        User.objects.create_user(
            email="CONSTRAINT@example.com",
            password="A-complex-password-2026!",
            status=User.Status.ACTIVE,
        )

    expect_integrity_error(duplicate)


def test_patient_digital_id_is_unique():
    require_postgresql()
    user = UserFactory(status="ACTIVE")
    PatientProfile.objects.create(
        user=user,
        digital_id="11111111111111111",
        full_name="One",
        date_of_birth=date(1990, 1, 1),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )

    def duplicate():
        PatientProfile.objects.create(
            user=UserFactory(status="ACTIVE"),
            digital_id="11111111111111111",
            full_name="Two",
            date_of_birth=date(1991, 1, 1),
            sex=PatientProfile.Sex.UNSPECIFIED,
            nationality="IQ",
        )

    expect_integrity_error(duplicate)


def test_claim_one_active_per_patient():
    require_postgresql()
    profile = verified_adult()

    def make_claim(email):
        return PatientAccountClaim.objects.create(
            patient=profile,
            requested_email=email,
            requested_phone="+9647000000000",
            submitted_name="Claimant",
            submitted_date_of_birth=date(1990, 1, 2),
            status=PatientAccountClaim.Status.PENDING,
        )

    make_claim("constraint-claim@example.com")

    def duplicate():
        make_claim("constraint-claim-2@example.com")

    expect_integrity_error(duplicate)


def test_identity_one_verified_current_type():
    require_postgresql()
    from identities.services import submit_identity_document

    user, profile, _ = create_verified_guardian()
    pending = submit_identity_document(
        patient=profile,
        actor=user,
        validated_data={
            "document_type": "PASSPORT",
            "document_number": "PASSPORT-1",
            "issuing_country": "IQ",
            "front_image": image_upload("passport-front.png"),
        },
    )
    # Approve the passport so it becomes a second VERIFIED+CURRENT document
    # of a *different* type — allowed. Then force a same-type duplicate.
    from identities.services import approve_identity_document

    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    approve_identity_document(document=pending, agent=agent)

    def duplicate():
        # Bypass service rules; DB constraint must reject two VERIFIED+CURRENT
        # documents of the same type for one patient.
        IdentityDocument.objects.create(
            patient=profile,
            document_type=IdentityDocument.DocumentType.PASSPORT,
            document_number="PASSPORT-2",
            issuing_country="IQ",
            front_image=pending.front_image,
            verification_status=IdentityDocument.VerificationStatus.VERIFIED,
            status=IdentityDocument.LifecycleStatus.CURRENT,
        )

    expect_integrity_error(duplicate)


def test_guardian_one_active_relationship_type():
    require_postgresql()
    guardian_user, profile, _ = create_verified_guardian()
    child = patient_model().objects.create(
        user=UserFactory(status="ACTIVE"),
        digital_id="12345678901234588",
        full_name="Constraint Child",
        date_of_birth=date(2015, 1, 1),
        sex="UNSPECIFIED",
        nationality="IQ",
    )
    GuardianRelationship.objects.create(
        guardian_user=guardian_user,
        minor_patient=child,
        relationship=GuardianRelationship.Relationship.FATHER,
        verification_status=GuardianRelationship.VerificationStatus.VERIFIED,
        active=True,
    )

    def duplicate():
        GuardianRelationship.objects.create(
            guardian_user=guardian_user,
            minor_patient=child,
            relationship=GuardianRelationship.Relationship.FATHER,
            verification_status=GuardianRelationship.VerificationStatus.VERIFIED,
            active=True,
        )

    expect_integrity_error(duplicate)


def test_audit_log_is_immutable_through_application_guards():
    require_postgresql()
    from django.core.exceptions import ValidationError

    user = UserFactory(status="ACTIVE")
    entry = AuditLog.objects.create(
        actor=user,
        actor_type=AuditLog.ActorType.USER,
        action=AuditLog.Action.ACCOUNT_CREATED,
        resource_type="USER",
        resource_uuid=user.uuid,
        new_values={},
    )
    entry.refresh_from_db()
    with pytest.raises(ValidationError):
        entry.new_values = {"tampered": True}
        entry.save()
    with pytest.raises(ValidationError):
        entry.delete()
    entry.refresh_from_db()
    assert entry.new_values == {}
