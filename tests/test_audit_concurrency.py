"""
PostgreSQL-only concurrency tests for the M14 audit log (M14 #28).

Verifies that concurrent idempotent operations produce exactly one semantic
audit entry (no duplicates) and that audit writes are transactional with the
state change they describe.
"""

from datetime import date

import pytest
from django.db import connection, transaction

from accounts.models import User
from audit.models import AuditLog
from claims.services.review import approve_account_claim
from claims.services.submission import submit_account_claim
from documents.date_services import confirm_document_date
from identities.services import approve_identity_document
from tests.factories import UserFactory
from tests.test_account_claiming import payload, verified_adult
from tests.test_minors_guardians import (
    create_verified_guardian,
    image_upload,
    patient_model,
)
from tests.test_postgresql_concurrency import run_concurrently

pytestmark = [pytest.mark.postgresql, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    settings.IDENTITY_FILE_MAX_BYTES = 1024 * 1024


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only audit concurrency test")


def test_concurrent_same_candidate_date_confirm_writes_one_audit():
    require_postgresql()
    from processing.date_services import process_date_candidates
    from tests.test_date_processing import prepared_document

    document = prepared_document("Report Date: 14/03/2026\n")
    actor = document.patient.user
    process_date_candidates(str(document.uuid))
    candidate = document.date_candidates.get(is_current=True, is_suggested=True)

    results, failures = run_concurrently(
        lambda: confirm_document_date(
            document=document, actor=actor, candidate_id=candidate.uuid
        ),
        lambda: confirm_document_date(
            document=document, actor=actor, candidate_id=candidate.uuid
        ),
    )
    assert not failures
    assert len(results) == 2
    assert (
        AuditLog.objects.filter(
            action=AuditLog.Action.DATE_CONFIRMED,
            resource_uuid=document.uuid,
        ).count()
        == 1
    )


def test_concurrent_claim_approval_writes_one_claim_approved_audit():
    require_postgresql()
    from claims.models import PatientAccountClaim
    from claims.serializers import AccountClaimSubmissionSerializer

    profile = verified_adult()
    serializer = AccountClaimSubmissionSerializer(data=payload())
    assert serializer.is_valid(), serializer.errors
    submit_account_claim(serializer.validated_data)
    claim = PatientAccountClaim.objects.get(patient=profile)

    first = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    second = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    results, failures = run_concurrently(
        lambda: approve_account_claim(claim=claim, agent=first),
        lambda: approve_account_claim(claim=claim, agent=second),
    )
    assert len(results) == 1
    assert len(failures) == 1
    assert (
        AuditLog.objects.filter(
            action=AuditLog.Action.CLAIM_APPROVED,
            resource_uuid=claim.uuid,
        ).count()
        == 1
    )


def test_concurrent_identity_approval_replay_writes_one_verified_audit():
    require_postgresql()
    from identities.models import IdentityDocument

    user, profile, agent = create_verified_guardian()
    document = IdentityDocument.objects.get(patient=profile)

    def approve():
        return approve_identity_document(document=document, agent=agent)

    # First approval happened during create_verified_guardian; replay now.
    results, failures = run_concurrently(approve, approve)
    assert not failures
    assert (
        AuditLog.objects.filter(
            action=AuditLog.Action.IDENTITY_DOCUMENT_VERIFIED,
            resource_uuid=document.uuid,
        ).count()
        == 1
    )


def test_audit_writes_are_transactional_with_rollback():
    require_postgresql()
    from identities.services import reject_identity_document, submit_identity_document

    user = User.objects.create_user(
        email="audit-rollback-owner@example.com",
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    profile = patient_model().objects.create(
        user=user,
        digital_id="12345678901234577",
        full_name="Rollback Owner",
        date_of_birth=date(1990, 1, 2),
        sex="UNSPECIFIED",
        nationality="IQ",
    )
    pending = submit_identity_document(
        patient=profile,
        actor=user,
        validated_data={
            "document_type": "UNIFIED_NATIONAL_CARD",
            "document_number": "CARD-ROLLBACK",
            "issuing_country": "IQ",
            "front_image": image_upload("rollback-front.png"),
        },
    )
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    before = AuditLog.objects.filter(
        action=AuditLog.Action.IDENTITY_DOCUMENT_REJECTED,
        resource_uuid=pending.uuid,
    ).count()
    try:
        with transaction.atomic():
            reject_identity_document(
                document=pending, agent=agent, reason="Rollback test"
            )
            raise RuntimeError("abort")
    except RuntimeError:
        pass
    after = AuditLog.objects.filter(
        action=AuditLog.Action.IDENTITY_DOCUMENT_REJECTED,
        resource_uuid=pending.uuid,
    ).count()
    assert after == before
