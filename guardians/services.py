import hashlib
import json
from dataclasses import dataclass
from datetime import date

from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import User
from audit.models import AuditLog
from audit.services import record_audit
from guardians.exceptions import (
    GuardianNotVerified,
    GuardianRelationshipConflict,
    IdempotencyConflict,
)
from guardians.models import (
    GuardianEvidence,
    GuardianRelationship,
    GuardianRelationshipEvent,
    MinorCreationRequest,
)
from identities.models import IdentityDocument
from identities.services import (
    delete_identity_file_from_storage,
    inspect_identity_upload,
    persist_identity_upload,
    submit_identity_document,
)
from patients.models import PatientProfile
from patients.services import create_patient_profile


@dataclass(frozen=True)
class MinorCreationResult:
    minor: PatientProfile
    relationship: GuardianRelationship
    created: bool


def eligible_guardian_profile(user, *, for_update=False):
    if (
        user.role != User.Role.PATIENT
        or user.status != User.Status.ACTIVE
        or not user.is_active
    ):
        raise GuardianNotVerified()
    queryset = PatientProfile.objects
    if for_update:
        queryset = queryset.select_for_update()
    try:
        profile = queryset.get(user=user)
    except PatientProfile.DoesNotExist as exc:
        raise GuardianNotVerified() from exc
    if (
        profile.is_minor
        or profile.identity_status != PatientProfile.IdentityStatus.VERIFIED
    ):
        raise GuardianNotVerified()
    has_card = IdentityDocument.objects.filter(
        patient=profile,
        document_type=IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
        status=IdentityDocument.LifecycleStatus.CURRENT,
        verification_status=IdentityDocument.VerificationStatus.VERIFIED,
    ).exists()
    if not has_card:
        raise GuardianNotVerified()
    return profile


def _request_fingerprint(validated_data):
    identity = validated_data["identity_data"]
    payload = {
        "profile": validated_data["profile_data"],
        "relationship": validated_data["relationship"],
        "identity": {
            key: value
            for key, value in identity.items()
            if key not in {"front_image", "back_image"}
        },
        "front_sha256": inspect_identity_upload(identity["front_image"]).sha256,
        "back_sha256": inspect_identity_upload(identity["back_image"]).sha256
        if identity.get("back_image")
        else None,
        "evidence_type": validated_data.get("evidence_type"),
        "evidence_sha256": inspect_identity_upload(
            validated_data["evidence_file"]
        ).sha256
        if validated_data.get("evidence_file")
        else None,
    }

    def default(value):
        if isinstance(value, date):
            return value.isoformat()
        return str(value)

    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=default
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _family_number_result(guardian_profile, identity_data, relationship_type):
    if relationship_type not in {
        GuardianRelationship.Relationship.FATHER,
        GuardianRelationship.Relationship.MOTHER,
    }:
        return GuardianRelationship.FamilyNumberResult.UNAVAILABLE
    if (
        identity_data["document_type"]
        != IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD
    ):
        return GuardianRelationship.FamilyNumberResult.UNAVAILABLE
    child_family = identity_data.get("family_number", "")
    guardian_family = (
        IdentityDocument.objects.filter(
            patient=guardian_profile,
            document_type=IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
            status=IdentityDocument.LifecycleStatus.CURRENT,
            verification_status=IdentityDocument.VerificationStatus.VERIFIED,
        )
        .values_list("family_number", flat=True)
        .first()
        or ""
    )
    if not child_family or not guardian_family:
        return GuardianRelationship.FamilyNumberResult.UNAVAILABLE
    if child_family == guardian_family:
        return GuardianRelationship.FamilyNumberResult.MATCH
    return GuardianRelationship.FamilyNumberResult.MISMATCH


def create_minor(*, guardian, idempotency_key, validated_data):
    request_hash = _request_fingerprint(validated_data)
    stored_files = []
    try:
        with transaction.atomic():
            guardian_profile = eligible_guardian_profile(guardian, for_update=True)
            creation, _ = MinorCreationRequest.objects.get_or_create(
                guardian_user=guardian,
                idempotency_key=idempotency_key,
                defaults={"request_hash": request_hash},
            )
            creation = MinorCreationRequest.objects.select_for_update().get(
                pk=creation.pk
            )
            if creation.request_hash != request_hash:
                raise IdempotencyConflict()
            if creation.minor_patient_id:
                return MinorCreationResult(
                    creation.minor_patient, creation.relationship, False
                )

            minor = create_patient_profile(user=None, **validated_data["profile_data"])
            document = submit_identity_document(
                patient=minor,
                actor=guardian,
                validated_data=dict(validated_data["identity_data"]),
            )
            minor.refresh_from_db()
            stored_files.extend([document.front_image, document.back_image])
            family_result = _family_number_result(
                guardian_profile,
                validated_data["identity_data"],
                validated_data["relationship"],
            )
            relationship = GuardianRelationship.objects.create(
                guardian_user=guardian,
                minor_patient=minor,
                relationship=validated_data["relationship"],
                family_number_result=family_result,
            )
            if evidence_upload := validated_data.get("evidence_file"):
                evidence_file = persist_identity_upload(evidence_upload)
                stored_files.append(evidence_file)
                GuardianEvidence.objects.create(
                    relationship=relationship,
                    evidence_type=validated_data["evidence_type"],
                    file=evidence_file,
                    metadata={},
                )
            for event_type in (
                GuardianRelationshipEvent.EventType.MINOR_CREATED,
                GuardianRelationshipEvent.EventType.SUBMITTED,
                GuardianRelationshipEvent.EventType.DOCUMENT_SUBMITTED,
            ):
                GuardianRelationshipEvent.objects.create(
                    relationship=relationship,
                    event_type=event_type,
                    actor=guardian,
                    metadata={},
                )
            family_event = {
                GuardianRelationship.FamilyNumberResult.MATCH: (
                    GuardianRelationshipEvent.EventType.FAMILY_MATCHED
                ),
                GuardianRelationship.FamilyNumberResult.MISMATCH: (
                    GuardianRelationshipEvent.EventType.FAMILY_MISMATCHED
                ),
            }.get(family_result)
            if family_event:
                GuardianRelationshipEvent.objects.create(
                    relationship=relationship,
                    event_type=family_event,
                    actor=guardian,
                    metadata={},
                )
            creation.minor_patient = minor
            creation.relationship = relationship
            creation.save(update_fields=("minor_patient", "relationship", "updated_at"))
            record_audit(
                action=AuditLog.Action.MINOR_CREATED,
                actor=guardian,
                patient=minor,
                resource_type="GUARDIAN_RELATIONSHIP",
                resource_uuid=relationship.uuid,
                new_values={"relationship": relationship.relationship},
            )
            record_audit(
                action=AuditLog.Action.GUARDIAN_RELATIONSHIP_SUBMITTED,
                actor=guardian,
                patient=minor,
                resource_type="GUARDIAN_RELATIONSHIP",
                resource_uuid=relationship.uuid,
                new_values={"relationship": relationship.relationship},
            )
            return MinorCreationResult(minor, relationship, True)
    except Exception:
        for stored_file in stored_files:
            delete_identity_file_from_storage(stored_file)
        raise


def submit_guardian_relationship(*, guardian, minor, relationship_type):
    guardian_profile = eligible_guardian_profile(guardian)
    if not minor.is_minor or minor.user_id is not None:
        raise GuardianRelationshipConflict()
    child_family = (
        IdentityDocument.objects.filter(
            patient=minor,
            document_type=IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
            status=IdentityDocument.LifecycleStatus.CURRENT,
        )
        .values_list("family_number", flat=True)
        .first()
        or ""
    )
    family_result = _family_number_result(
        guardian_profile,
        {
            "document_type": IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
            "family_number": child_family,
        },
        relationship_type,
    )
    if GuardianRelationship.objects.filter(
        guardian_user=guardian,
        minor_patient=minor,
        relationship=relationship_type,
        verification_status__in=(
            GuardianRelationship.VerificationStatus.PENDING,
            GuardianRelationship.VerificationStatus.VERIFIED,
        ),
    ).exists():
        raise GuardianRelationshipConflict()
    try:
        with transaction.atomic():
            relationship = GuardianRelationship.objects.create(
                guardian_user=guardian,
                minor_patient=minor,
                relationship=relationship_type,
                family_number_result=family_result,
            )
            GuardianRelationshipEvent.objects.create(
                relationship=relationship,
                event_type=GuardianRelationshipEvent.EventType.SUBMITTED,
                actor=guardian,
                metadata={},
            )
            record_audit(
                action=AuditLog.Action.GUARDIAN_RELATIONSHIP_SUBMITTED,
                actor=guardian,
                patient=minor,
                resource_type="GUARDIAN_RELATIONSHIP",
                resource_uuid=relationship.uuid,
                new_values={"relationship": relationship.relationship},
            )
            return relationship
    except IntegrityError as exc:
        raise GuardianRelationshipConflict() from exc


def _require_agent(agent):
    if agent.role != User.Role.IDENTITY_VERIFICATION_AGENT:
        from identities.exceptions import VerificationAgentRequired

        raise VerificationAgentRequired()


def approve_guardian_relationship(*, relationship, agent):
    _require_agent(agent)
    with transaction.atomic():
        relationship = (
            GuardianRelationship.objects.select_for_update()
            .select_related("minor_patient", "guardian_user")
            .get(pk=relationship.pk)
        )
        if (
            relationship.verification_status
            == GuardianRelationship.VerificationStatus.VERIFIED
        ):
            if relationship.active and relationship.verified_by_id == agent.pk:
                return relationship
            raise GuardianRelationshipConflict()
        if (
            relationship.verification_status
            != GuardianRelationship.VerificationStatus.PENDING
        ):
            raise GuardianRelationshipConflict()
        eligible_guardian_profile(relationship.guardian_user, for_update=True)
        minor = PatientProfile.objects.select_for_update().get(
            pk=relationship.minor_patient_id
        )
        if not minor.is_minor:
            raise GuardianRelationshipConflict()
        if not IdentityDocument.objects.filter(
            patient=minor,
            document_type__in=(
                IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
                IdentityDocument.DocumentType.BIRTH_DOCUMENT,
            ),
            status=IdentityDocument.LifecycleStatus.CURRENT,
            verification_status=IdentityDocument.VerificationStatus.VERIFIED,
        ).exists():
            raise GuardianRelationshipConflict()
        if (
            relationship.relationship
            == GuardianRelationship.Relationship.LEGAL_GUARDIAN
            and not relationship.evidences.exists()
        ):
            raise GuardianRelationshipConflict()
        relationship.verification_status = (
            GuardianRelationship.VerificationStatus.VERIFIED
        )
        relationship.active = True
        relationship.verified_by = agent
        relationship.verified_at = timezone.now()
        relationship.rejection_reason = ""
        relationship.save(
            update_fields=(
                "verification_status",
                "active",
                "verified_by",
                "verified_at",
                "rejection_reason",
                "updated_at",
            )
        )
        GuardianRelationshipEvent.objects.create(
            relationship=relationship,
            event_type=GuardianRelationshipEvent.EventType.VERIFIED,
            actor=agent,
            metadata={},
        )
        record_audit(
            action=AuditLog.Action.GUARDIAN_RELATIONSHIP_VERIFIED,
            actor=agent,
            patient=relationship.minor_patient,
            resource_type="GUARDIAN_RELATIONSHIP",
            resource_uuid=relationship.uuid,
            previous_values={
                "verification_status": GuardianRelationship.VerificationStatus.PENDING
            },
            new_values={
                "verification_status": GuardianRelationship.VerificationStatus.VERIFIED
            },
        )
        return relationship


def reject_guardian_relationship(*, relationship, agent, reason):
    _require_agent(agent)
    with transaction.atomic():
        relationship = GuardianRelationship.objects.select_for_update().get(
            pk=relationship.pk
        )
        if (
            relationship.verification_status
            == GuardianRelationship.VerificationStatus.REJECTED
        ):
            if (
                relationship.verified_by_id == agent.pk
                and relationship.rejection_reason == reason
            ):
                return relationship
            raise GuardianRelationshipConflict()
        if (
            relationship.verification_status
            != GuardianRelationship.VerificationStatus.PENDING
        ):
            raise GuardianRelationshipConflict()
        relationship.verification_status = (
            GuardianRelationship.VerificationStatus.REJECTED
        )
        relationship.active = False
        relationship.verified_by = agent
        relationship.verified_at = timezone.now()
        relationship.rejection_reason = reason
        relationship.save(
            update_fields=(
                "verification_status",
                "active",
                "verified_by",
                "verified_at",
                "rejection_reason",
                "updated_at",
            )
        )
        GuardianRelationshipEvent.objects.create(
            relationship=relationship,
            event_type=GuardianRelationshipEvent.EventType.REJECTED,
            actor=agent,
            metadata={},
        )
        record_audit(
            action=AuditLog.Action.GUARDIAN_RELATIONSHIP_REJECTED,
            actor=agent,
            patient=relationship.minor_patient,
            resource_type="GUARDIAN_RELATIONSHIP",
            resource_uuid=relationship.uuid,
            previous_values={
                "verification_status": GuardianRelationship.VerificationStatus.PENDING
            },
            new_values={
                "verification_status": GuardianRelationship.VerificationStatus.REJECTED
            },
        )
        return relationship


def guardian_can_access_minor(user, minor):
    if (
        user.role != User.Role.PATIENT
        or user.status != User.Status.ACTIVE
        or not user.is_active
        or not minor.is_minor
    ):
        return False
    try:
        eligible_guardian_profile(user)
    except GuardianNotVerified:
        return False
    return GuardianRelationship.objects.filter(
        guardian_user=user,
        minor_patient=minor,
        verification_status=GuardianRelationship.VerificationStatus.VERIFIED,
        active=True,
    ).exists()
