"""Privileged, policy-driven account purge for Django Operations."""

from dataclasses import dataclass
from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import ProtectedError, Q
from django.utils import timezone

from audit.models import AuditLog
from audit.services import record_audit

PURGE_REASONS = (
    ("TEST_ACCOUNT_CLEANUP", "Test account cleanup"),
    ("DUPLICATE_ACCOUNT_CLEANUP", "Duplicate account cleanup"),
    ("USER_REQUESTED_DELETION", "User-requested deletion"),
    ("SECURITY_REMEDIATION", "Security remediation"),
    ("ADMINISTRATIVE_CORRECTION", "Administrative correction"),
)
PURGE_REASON_CODES = {code for code, _label in PURGE_REASONS}


class AccountPurgeBlocked(ValidationError):
    pass


@dataclass(frozen=True)
class AccountPurgePreview:
    target_uuid: str
    email: str
    counts: dict[str, int]


@dataclass(frozen=True)
class AccountPurgeResult:
    target_uuid: str
    status: str
    counts: dict[str, int]


def can_system_purge_users(user):
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and user.is_active
        and user.is_superuser
    )


def is_last_active_superuser(user):
    User = get_user_model()
    return bool(
        user.is_active
        and user.is_superuser
        and User.objects.filter(is_active=True, is_superuser=True).count() <= 1
    )


def _validate_reason(reason, reason_detail):
    reason = (reason or "").strip()
    reason_detail = (reason_detail or "").strip()
    if reason not in PURGE_REASON_CODES:
        raise AccountPurgeBlocked("Select a valid purge reason.")
    if len(reason_detail) > 200:
        raise AccountPurgeBlocked(
            "Purge reason detail must be 200 characters or fewer."
        )
    return reason, reason_detail


def _target_profile(target, *, lock=False):
    from patients.models import PatientProfile

    queryset = PatientProfile.objects.filter(user=target)
    if lock:
        queryset = queryset.select_for_update()
    return queryset.first()


def preview_user_purge(*, actor, target):
    if not can_system_purge_users(actor):
        raise PermissionDenied("System purge requires an active superuser.")
    profile = _target_profile(target)
    from claims.models import PatientAccountClaim
    from documents.models import MedicalDocument
    from guardians.models import GuardianRelationship
    from identities.models import IdentityDocument

    relationships = GuardianRelationship.objects.filter(
        Q(guardian_user=target)
        | (Q(minor_patient=profile) if profile else Q(pk__isnull=True))
    )
    claims = PatientAccountClaim.objects.filter(
        (Q(patient=profile) if profile else Q(pk__isnull=True))
        | Q(approved_user=target)
    )
    counts = {
        "profiles": int(profile is not None),
        "identity_documents": IdentityDocument.objects.filter(patient=profile).count()
        if profile
        else 0,
        "medical_documents": MedicalDocument.objects.filter(patient=profile).count()
        if profile
        else 0,
        "guardian_relationships": relationships.count(),
        "claims": claims.count(),
    }
    return AccountPurgePreview(str(target.pk), target.email, counts)


def _delete_target_sessions(target_uuid):
    target_id = str(target_uuid)
    for session in Session.objects.iterator():
        try:
            if str(session.get_decoded().get("_auth_user_id", "")) == target_id:
                session.delete()
        except Exception:
            continue


def _schedule_storage_delete(storage, name):
    if name:
        transaction.on_commit(lambda storage=storage, name=name: storage.delete(name))


def purge_user_account_as_superuser(*, actor, target, reason, reason_detail=""):
    """Purge one account while retaining scrubbed immutable domain history."""
    if not can_system_purge_users(actor):
        raise PermissionDenied("System purge requires an active superuser.")
    reason, reason_detail = _validate_reason(reason, reason_detail)
    if actor.pk == target.pk:
        raise AccountPurgeBlocked("Self-purge is blocked.")

    User = get_user_model()
    target_uuid = str(target.pk)
    preview = preview_user_purge(actor=actor, target=target)
    initial_profile = _target_profile(target)
    audit_metadata = {"reason": reason, "counts": preview.counts}
    record_audit(
        action=AuditLog.SUPERUSER_ACCOUNT_PURGE_REQUESTED,
        actor=actor,
        patient=initial_profile,
        resource_type="USER",
        resource_uuid=target.pk,
        metadata=audit_metadata,
    )

    with transaction.atomic():
        actor = User.objects.select_for_update().get(pk=actor.pk)
        target = User.objects.select_for_update().get(pk=target.pk)
        if not can_system_purge_users(actor):
            raise PermissionDenied("System purge requires an active superuser.")
        tombstone_email = f"purged+{target.pk.hex}@invalid.pmdap.local"
        if not target.is_active and target.email == tombstone_email:
            raise AccountPurgeBlocked("Account is already purged.")
        active_superuser_ids = list(
            User.objects.select_for_update()
            .filter(is_active=True, is_superuser=True)
            .values_list("pk", flat=True)
        )
        if target.is_active and target.is_superuser and len(active_superuser_ids) <= 1:
            raise AccountPurgeBlocked("Last active superuser purge is blocked.")

        profile = _target_profile(target, lock=True)

        _delete_target_sessions(target.pk)
        try:
            from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

            OutstandingToken.objects.filter(user=target).delete()
        except ImportError:  # pragma: no cover - optional package guard
            pass

        from identities.extraction_store import clear_extraction_result
        from identities.models import (
            IdentityDocument,
            IdentityExtractionJob,
            IdentityFile,
        )
        from identities.storage import private_identity_storage

        jobs = IdentityExtractionJob.objects.select_for_update().filter(user=target)
        for job in jobs:
            for key in (job.front_key, job.back_key):
                _schedule_storage_delete(private_identity_storage, key)
            transaction.on_commit(
                lambda job_uuid=job.uuid: clear_extraction_result(job_uuid)
            )
        IdentityExtractionJob.objects.filter(user=target).delete()

        from documents.models import MedicalDocument
        from documents.services import purge_medical_document

        if profile:
            for document in list(
                MedicalDocument.objects.select_for_update().filter(patient=profile)
            ):
                purge_medical_document(document=document)
        identity_file_ids = set()
        identity_document_ids = set()
        if profile:
            documents = IdentityDocument.objects.select_for_update().filter(
                patient=profile
            )
            for document in documents:
                identity_document_ids.add(document.pk)
                identity_file_ids.update(
                    pk for pk in (document.front_image_id, document.back_image_id) if pk
                )
            documents.update(
                document_number="",
                national_number="",
                family_number="",
                unique_card_body_number="",
                issuing_country="XX",
                issue_date=None,
                expiry_date=None,
                verification_status=IdentityDocument.VerificationStatus.REJECTED,
                status=IdentityDocument.LifecycleStatus.REVOKED,
                verified_at=None,
                rejection_reason="",
            )

        from guardians.models import (
            GuardianEvidence,
            GuardianRelationship,
            GuardianRelationshipEvent,
            MinorCreationRequest,
        )

        relationship_filter = Q(guardian_user=target)
        if profile:
            relationship_filter |= Q(minor_patient=profile)
        relationships = list(
            GuardianRelationship.objects.select_for_update().filter(relationship_filter)
        )
        relationship_ids = [relationship.pk for relationship in relationships]
        now = timezone.now()
        for relationship in relationships:
            if relationship.active or relationship.ended_at is None:
                GuardianRelationship.objects.filter(pk=relationship.pk).update(
                    active=False,
                    ended_at=now,
                    ended_reason=GuardianRelationship.EndedReason.ADMINISTRATIVE_CORRECTION,
                    ended_reason_detail="",
                )
                GuardianRelationshipEvent.objects.create(
                    relationship=relationship,
                    event_type=GuardianRelationshipEvent.EventType.ENDED,
                    actor=actor,
                    metadata={"reason": "SUPERUSER_ACCOUNT_PURGE"},
                )
        evidences = GuardianEvidence.objects.filter(
            relationship_id__in=relationship_ids
        )
        identity_file_ids.update(evidences.values_list("file_id", flat=True))
        evidences.delete()
        MinorCreationRequest.objects.filter(
            Q(guardian_user=target)
            | Q(relationship_id__in=relationship_ids)
            | (Q(minor_patient=profile) if profile else Q(pk__isnull=True))
        ).delete()
        GuardianRelationship.objects.filter(pk__in=relationship_ids).update(
            **{
                "verified_by": None,
                "family_number_result": (
                    GuardianRelationship.FamilyNumberResult.UNAVAILABLE
                ),
                "name_evidence_result": (
                    GuardianRelationship.NameEvidenceResult.UNAVAILABLE
                ),
                "evidence_checked_at": None,
                "evidence_policy_version": "",
                "active": False,
                "ended_reason_detail": "",
                "rejection_reason": "",
            }
        )

        from claims.models import (
            AccountActivation,
            ClaimIdentityEvidence,
            PatientAccountClaim,
        )

        claim_filter = Q(approved_user=target)
        if profile:
            claim_filter |= Q(patient=profile)
        claims = PatientAccountClaim.objects.select_for_update().filter(claim_filter)
        claim_ids = list(claims.values_list("pk", flat=True))
        claim_evidence = ClaimIdentityEvidence.objects.filter(claim_id__in=claim_ids)
        identity_file_ids.update(
            claim_evidence.values_list("front_image_id", flat=True)
        )
        identity_file_ids.update(
            pk for pk in claim_evidence.values_list("back_image_id", flat=True) if pk
        )
        claim_evidence.delete()
        AccountActivation.objects.filter(
            Q(claim_id__in=claim_ids) | Q(user=target)
        ).delete()
        claims.update(
            requested_email="",
            requested_phone="",
            submitted_name="",
            submitted_date_of_birth=date(1900, 1, 1),
            status=PatientAccountClaim.Status.CANCELLED,
            name_comparison=PatientAccountClaim.Comparison.UNAVAILABLE,
            date_of_birth_comparison=PatientAccountClaim.Comparison.UNAVAILABLE,
            document_number_comparison=PatientAccountClaim.Comparison.UNAVAILABLE,
            reviewed_at=None,
            rejection_reason="",
            review_notes="",
        )

        external_document_file = IdentityDocument.objects.filter(
            Q(front_image_id__in=identity_file_ids)
            | Q(back_image_id__in=identity_file_ids)
        ).exclude(pk__in=identity_document_ids)
        external_guardian_file = GuardianEvidence.objects.filter(
            file_id__in=identity_file_ids
        ).exclude(relationship_id__in=relationship_ids)
        external_claim_file = ClaimIdentityEvidence.objects.filter(
            Q(front_image_id__in=identity_file_ids)
            | Q(back_image_id__in=identity_file_ids)
        ).exclude(claim_id__in=claim_ids)
        if (
            external_document_file.exists()
            or external_guardian_file.exists()
            or external_claim_file.exists()
        ):
            raise AccountPurgeBlocked("Shared identity file prevents safe purge.")

        for identity_file in IdentityFile.objects.filter(pk__in=identity_file_ids):
            _schedule_storage_delete(
                identity_file.file.storage, identity_file.file.name
            )
        IdentityFile.objects.filter(pk__in=identity_file_ids).update(
            file="",
            original_name="",
            media_type="",
            size=0,
            sha256="",
        )
        for identity_file in IdentityFile.objects.filter(pk__in=identity_file_ids):
            try:
                identity_file.delete()
            except ProtectedError:
                pass

        if profile:
            if profile.avatar and profile.avatar.name:
                _schedule_storage_delete(profile.avatar.storage, profile.avatar.name)
            type(profile).objects.filter(pk=profile.pk).update(
                digital_id=f"P{profile.pk.hex[:16]}",
                full_name="Purged account",
                given_name="",
                father_name="",
                grandfather_name="",
                mother_name="",
                governorate="",
                date_of_birth=date(1900, 1, 1),
                sex=profile.Sex.UNSPECIFIED,
                nationality="ZZ",
                blood_group=profile.BloodGroup.UNKNOWN,
                identity_status=profile.IdentityStatus.UNVERIFIED,
                avatar="",
            )

        target.email = tombstone_email
        target.phone = ""
        target.first_name = ""
        target.last_name = ""
        target.status = User.Status.DISABLED
        target.email_verified = False
        target.phone_verified = False
        target.is_active = False
        target.is_staff = False
        target.is_superuser = False
        target.set_unusable_password()
        target.save(
            update_fields=(
                "email",
                "phone",
                "first_name",
                "last_name",
                "status",
                "email_verified",
                "phone_verified",
                "is_active",
                "is_staff",
                "is_superuser",
                "password",
                "updated_at",
            )
        )
        record_audit(
            action=AuditLog.SUPERUSER_ACCOUNT_PURGE_COMPLETED,
            actor=actor,
            actor_type=AuditLog.ActorType.USER,
            resource_type="USER",
            resource_uuid=target_uuid,
            metadata=audit_metadata,
        )

    return AccountPurgeResult(target_uuid, "SUCCESS", preview.counts)
