import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from audit.models import AuditLog
from audit.services import record_audit
from claims.exceptions import (
    AccountClaimConflict,
    AccountClaimNotFound,
    VerificationAgentRequired,
)
from claims.models import (
    AccountActivation,
    PatientAccountClaim,
    PatientAccountClaimEvent,
)
from guardians.models import GuardianRelationship, GuardianRelationshipEvent
from identities.models import IdentityDocument
from patients.models import PatientProfile

User = get_user_model()


@dataclass(frozen=True)
class ApprovalResult:
    claim_id: object
    user_id: object
    status: str
    activation_token: str
    activation_expires_at: object


def require_agent(user):
    if user.role != User.Role.IDENTITY_VERIFICATION_AGENT:
        raise VerificationAgentRequired()


def get_claim(user, claim_uuid, *, lock=False):
    require_agent(user)
    queryset = PatientAccountClaim.objects.select_related("patient").prefetch_related(
        "identity_evidence__front_image",
        "identity_evidence__back_image",
        "patient__identity_documents",
        "patient__guardian_relationships",
    )
    if lock:
        queryset = queryset.select_for_update()
    try:
        return queryset.get(uuid=claim_uuid)
    except (PatientAccountClaim.DoesNotExist, ValueError) as exc:
        raise AccountClaimNotFound() from exc


def transition_claim(*, claim, agent, status, reason):
    require_agent(agent)
    with transaction.atomic():
        claim = get_claim(agent, claim.uuid, lock=True)
        if claim.status not in (
            PatientAccountClaim.Status.PENDING,
            PatientAccountClaim.Status.UNDER_REVIEW,
            PatientAccountClaim.Status.MORE_INFORMATION_REQUIRED,
        ):
            raise AccountClaimConflict()
        now = timezone.now()
        if claim.status == PatientAccountClaim.Status.PENDING:
            PatientAccountClaimEvent.objects.create(
                claim=claim,
                event_type=PatientAccountClaimEvent.EventType.UNDER_REVIEW,
                actor=agent,
                metadata={},
            )
        claim.status = status
        claim.reviewed_by = agent
        claim.reviewed_at = now
        claim.rejection_reason = reason
        claim.save(
            update_fields=(
                "status",
                "reviewed_by",
                "reviewed_at",
                "rejection_reason",
                "updated_at",
            )
        )
        event_type = (
            PatientAccountClaimEvent.EventType.REJECTED
            if status == PatientAccountClaim.Status.REJECTED
            else PatientAccountClaimEvent.EventType.MORE_INFORMATION_REQUIRED
        )
        PatientAccountClaimEvent.objects.create(
            claim=claim, event_type=event_type, actor=agent, metadata={"reason": reason}
        )
        record_audit(
            action=(
                AuditLog.Action.CLAIM_REJECTED
                if status == PatientAccountClaim.Status.REJECTED
                else AuditLog.Action.CLAIM_MORE_INFORMATION_REQUIRED
            ),
            actor=agent,
            patient=claim.patient,
            resource_type="ACCOUNT_CLAIM",
            resource_uuid=claim.uuid,
            previous_values={"status": "PENDING"},
            new_values={"status": status},
            metadata={"reason_code": "review_decision"},
        )
        return claim


def approve_account_claim(*, claim, agent):
    require_agent(agent)
    with transaction.atomic():
        claim = get_claim(agent, claim.uuid, lock=True)
        if claim.status not in (
            PatientAccountClaim.Status.PENDING,
            PatientAccountClaim.Status.UNDER_REVIEW,
            PatientAccountClaim.Status.MORE_INFORMATION_REQUIRED,
        ):
            raise AccountClaimConflict()
        if claim.status == PatientAccountClaim.Status.PENDING:
            PatientAccountClaimEvent.objects.create(
                claim=claim,
                event_type=PatientAccountClaimEvent.EventType.UNDER_REVIEW,
                actor=agent,
                metadata={},
            )
        profile = PatientProfile.objects.select_for_update().get(pk=claim.patient_id)
        valid_identity = IdentityDocument.objects.filter(
            patient=profile,
            document_type=IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
            status=IdentityDocument.LifecycleStatus.CURRENT,
            verification_status=IdentityDocument.VerificationStatus.VERIFIED,
        ).exists()
        if (
            profile.user_id is not None
            or profile.is_minor
            or profile.identity_status != PatientProfile.IdentityStatus.VERIFIED
            or not valid_identity
            or User.objects.filter(email__iexact=claim.requested_email).exists()
        ):
            raise AccountClaimConflict()
        user = User.objects.create_user(
            email=claim.requested_email,
            password=None,
            phone=claim.requested_phone,
            role=User.Role.PATIENT,
            status=User.Status.PENDING_ACTIVATION,
        )
        profile.user = user
        profile.full_clean()
        profile.save(update_fields=("user", "updated_at"))
        PatientAccountClaimEvent.objects.create(
            claim=claim,
            event_type=PatientAccountClaimEvent.EventType.PATIENT_LINKED,
            actor=agent,
            metadata={},
        )
        record_audit(
            action=AuditLog.Action.PATIENT_ACCOUNT_LINKED,
            actor=agent,
            patient=profile,
            resource_type="USER",
            resource_uuid=user.uuid,
            new_values={"status": user.status},
        )
        now = timezone.now()
        relationships = GuardianRelationship.objects.select_for_update().filter(
            minor_patient=profile, active=True
        )
        for relationship in relationships:
            relationship.active = False
            relationship.ended_at = now
            relationship.ended_reason = (
                GuardianRelationship.EndedReason.PATIENT_REACHED_ADULTHOOD
            )
            relationship.save(
                update_fields=("active", "ended_at", "ended_reason", "updated_at")
            )
            GuardianRelationshipEvent.objects.create(
                relationship=relationship,
                event_type=GuardianRelationshipEvent.EventType.ENDED,
                actor=agent,
                metadata={"reason": "PATIENT_REACHED_ADULTHOOD"},
            )
            record_audit(
                action=AuditLog.Action.GUARDIAN_ACCESS_EXPIRED,
                actor=agent,
                patient=profile,
                resource_type="GUARDIAN_RELATIONSHIP",
                resource_uuid=relationship.uuid,
                new_values={"active": False},
                metadata={"reason": "PATIENT_REACHED_ADULTHOOD"},
            )
        raw_token = secrets.token_urlsafe(32)
        expires_at = now + timedelta(
            minutes=getattr(settings, "ACCOUNT_CLAIM_ACTIVATION_MINUTES", 30)
        )
        AccountActivation.objects.create(
            claim=claim,
            user=user,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            expires_at=expires_at,
        )
        PatientAccountClaimEvent.objects.create(
            claim=claim,
            event_type=PatientAccountClaimEvent.EventType.ACTIVATION_CREATED,
            actor=agent,
            metadata={},
        )
        record_audit(
            action=AuditLog.Action.ACCOUNT_ACTIVATION_CREATED,
            actor=agent,
            patient=profile,
            resource_type="ACCOUNT_CLAIM",
            resource_uuid=claim.uuid,
            new_values={"expires_minutes": settings.ACCOUNT_CLAIM_ACTIVATION_MINUTES},
        )
        claim.status = PatientAccountClaim.Status.APPROVED
        claim.reviewed_by = agent
        claim.reviewed_at = now
        claim.approved_user = user
        claim.rejection_reason = ""
        claim.save(
            update_fields=(
                "status",
                "reviewed_by",
                "reviewed_at",
                "approved_user",
                "rejection_reason",
                "updated_at",
            )
        )
        PatientAccountClaimEvent.objects.create(
            claim=claim,
            event_type=PatientAccountClaimEvent.EventType.APPROVED,
            actor=agent,
            metadata={},
        )
        record_audit(
            action=AuditLog.Action.CLAIM_APPROVED,
            actor=agent,
            patient=profile,
            resource_type="ACCOUNT_CLAIM",
            resource_uuid=claim.uuid,
            previous_values={"status": "PENDING"},
            new_values={"status": claim.status},
        )
        return ApprovalResult(
            claim.uuid, user.uuid, claim.status, raw_token, expires_at
        )
