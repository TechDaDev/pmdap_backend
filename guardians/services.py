import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date

from django.db import IntegrityError, transaction
from django.db.models import Q
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
from identities.exceptions import IdentityExtractionJobNotFound
from identities.extraction_store import read_extraction_result
from identities.models import IdentityDocument, IdentityExtractionJob
from identities.permissions import can_verify_identity
from identities.serializers import IdentityDocumentInputSerializer
from identities.services import (
    delete_identity_file_from_storage,
    finalize_identity_document,
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


EVIDENCE_POLICY_VERSION = "M27_V1"


def normalize_family_number(value):
    return re.sub(r"\s+", "", (value or "").strip()).upper()


def _normalize_name(value):
    return " ".join((value or "").split()).casefold()


def _authoritative_card(profile):
    return (
        IdentityDocument.objects.filter(
            patient=profile,
            document_type=IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
            status=IdentityDocument.LifecycleStatus.CURRENT,
            verification_status=IdentityDocument.VerificationStatus.VERIFIED,
        )
        .order_by("-verified_at", "-created_at")
        .first()
    )


def evaluate_relationship_evidence(relationship, *, save=True):
    guardian_profile = relationship.guardian_user.patient_profile
    guardian_card = _authoritative_card(guardian_profile)
    minor_card = _authoritative_card(relationship.minor_patient)
    family_result = GuardianRelationship.FamilyNumberResult.UNAVAILABLE
    if (
        relationship.relationship
        in {
            GuardianRelationship.Relationship.FATHER,
            GuardianRelationship.Relationship.MOTHER,
        }
        and guardian_card
        and minor_card
    ):
        guardian_family = normalize_family_number(guardian_card.family_number)
        minor_family = normalize_family_number(minor_card.family_number)
        if guardian_family and minor_family:
            family_result = (
                GuardianRelationship.FamilyNumberResult.MATCH
                if guardian_family == minor_family
                else GuardianRelationship.FamilyNumberResult.MISMATCH
            )
    name_result = GuardianRelationship.NameEvidenceResult.UNAVAILABLE
    if relationship.relationship == GuardianRelationship.Relationship.FATHER:
        guardian_name = _normalize_name(guardian_profile.full_name)
        father_name = _normalize_name(relationship.minor_patient.father_name)
        if guardian_name and father_name:
            name_result = (
                GuardianRelationship.NameEvidenceResult.MATCH
                if guardian_name == father_name
                else GuardianRelationship.NameEvidenceResult.MISMATCH
            )
    relationship.family_number_result = family_result
    relationship.name_evidence_result = name_result
    relationship.guardian_identity_document = guardian_card
    relationship.minor_identity_document = minor_card
    relationship.evidence_checked_at = timezone.now()
    relationship.evidence_policy_version = EVIDENCE_POLICY_VERSION
    if save:
        relationship.save(
            update_fields=(
                "family_number_result",
                "name_evidence_result",
                "guardian_identity_document",
                "minor_identity_document",
                "evidence_checked_at",
                "evidence_policy_version",
                "updated_at",
            )
        )
    return relationship


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
        "front_sha256": inspect_identity_upload(identity["front_image"]).sha256
        if identity.get("front_image")
        else None,
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


def _extracted_value(payload, field):
    value = (payload.get("fields", {}).get(field) or {}).get("value")
    return str(value).strip() if value is not None else ""


def _authoritative_extraction_identity(job):
    payload = read_extraction_result(job.uuid)
    if payload is None:
        raise IdentityExtractionJobNotFound()
    national_number = _extracted_value(payload, "national_card_number")
    family_number = _extracted_value(payload, "family_number")
    card_body_number = _extracted_value(payload, "unique_card_body_number")
    missing = {
        field: ["Authoritative extraction did not produce this field."]
        for field, value in (
            ("national_number", national_number),
            ("family_number", family_number),
            ("unique_card_body_number", card_body_number),
        )
        if not value
    }
    if missing:
        from rest_framework import serializers

        raise serializers.ValidationError(missing)
    serializer = IdentityDocumentInputSerializer(
        data={
            "document_type": IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
            "document_number": national_number,
            "national_number": national_number,
            "family_number": family_number,
            "unique_card_body_number": card_body_number,
            "issuing_country": "IQ",
            "extraction_job_id": str(job.uuid),
        }
    )
    serializer.is_valid(raise_exception=True)
    identity_data = dict(serializer.validated_data)
    identity_data.pop("extraction_job_id", None)
    return identity_data


def create_minor(*, guardian, idempotency_key, validated_data):
    request_hash = _request_fingerprint(validated_data)
    stored_files = []
    try:
        with transaction.atomic():
            eligible_guardian_profile(guardian, for_update=True)
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

            extraction_job_id = validated_data["identity_data"].get("extraction_job_id")
            extraction_job = None
            identity_data = dict(validated_data["identity_data"])
            if extraction_job_id:
                try:
                    extraction_job = IdentityExtractionJob.objects.get(
                        uuid=extraction_job_id, user=guardian
                    )
                except IdentityExtractionJob.DoesNotExist:
                    raise IdentityExtractionJobNotFound() from None
                identity_data = _authoritative_extraction_identity(extraction_job)

            minor = create_patient_profile(user=None, **validated_data["profile_data"])
            if extraction_job is not None:
                document = finalize_identity_document(
                    patient=minor,
                    actor=guardian,
                    validated_data=identity_data,
                    job=extraction_job,
                    defer_cleanup=True,
                )
            else:
                document = submit_identity_document(
                    patient=minor,
                    actor=guardian,
                    validated_data=identity_data,
                )
            minor.refresh_from_db()
            stored_files.extend([document.front_image, document.back_image])
            relationship = GuardianRelationship.objects.create(
                guardian_user=guardian,
                minor_patient=minor,
                relationship=validated_data["relationship"],
                family_number_result=GuardianRelationship.FamilyNumberResult.UNAVAILABLE,
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
    eligible_guardian_profile(guardian)
    if minor.user_id == guardian.pk:
        raise GuardianRelationshipConflict()
    if not minor.is_minor or minor.user_id is not None:
        raise GuardianRelationshipConflict()
    if GuardianRelationship.objects.filter(
        guardian_user=guardian,
        minor_patient=minor,
        relationship=relationship_type,
        verification_status__in=(
            GuardianRelationship.VerificationStatus.PENDING,
            GuardianRelationship.VerificationStatus.VERIFIED,
        ),
        ended_at__isnull=True,
    ).exists():
        raise GuardianRelationshipConflict()
    try:
        with transaction.atomic():
            relationship = GuardianRelationship.objects.create(
                guardian_user=guardian,
                minor_patient=minor,
                relationship=relationship_type,
                family_number_result=GuardianRelationship.FamilyNumberResult.UNAVAILABLE,
            )
            evaluate_relationship_evidence(relationship)
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
    if not can_verify_identity(agent):
        from identities.exceptions import VerificationAgentRequired

        raise VerificationAgentRequired()


def approve_guardian_relationship(*, relationship, agent):
    _require_agent(agent)
    # Persist the latest evidence outcome for the review record even when the
    # subsequent locked transition is denied.
    relationship = GuardianRelationship.objects.select_related(
        "guardian_user__patient_profile", "minor_patient"
    ).get(pk=relationship.pk)
    evaluate_relationship_evidence(relationship)
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
        if minor.identity_status != PatientProfile.IdentityStatus.VERIFIED:
            raise GuardianRelationshipConflict()
        evaluate_relationship_evidence(relationship)
        if (
            relationship.relationship
            in {
                GuardianRelationship.Relationship.FATHER,
                GuardianRelationship.Relationship.MOTHER,
            }
            and relationship.family_number_result
            != GuardianRelationship.FamilyNumberResult.MATCH
        ):
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


def revoke_guardian_relationship(*, relationship, actor, reason):
    reason = (reason or "").strip()
    if not reason:
        raise GuardianRelationshipConflict()
    if actor.pk != relationship.guardian_user_id and not can_verify_identity(actor):
        from identities.exceptions import VerificationAgentRequired

        raise VerificationAgentRequired()
    with transaction.atomic():
        relationship = (
            GuardianRelationship.objects.select_for_update()
            .select_related("minor_patient")
            .get(pk=relationship.pk)
        )
        if not relationship.active or relationship.ended_at is not None:
            raise GuardianRelationshipConflict()
        relationship.active = False
        relationship.ended_at = timezone.now()
        relationship.ended_reason = GuardianRelationship.EndedReason.REVOKED
        relationship.ended_reason_detail = reason
        relationship.save(
            update_fields=(
                "active",
                "ended_at",
                "ended_reason",
                "ended_reason_detail",
                "updated_at",
            )
        )
        GuardianRelationshipEvent.objects.create(
            relationship=relationship,
            event_type=GuardianRelationshipEvent.EventType.ENDED,
            actor=actor,
            metadata={},
        )
        record_audit(
            action=AuditLog.Action.GUARDIAN_RELATIONSHIP_ENDED,
            actor=actor,
            patient=relationship.minor_patient,
            resource_type="GUARDIAN_RELATIONSHIP",
            resource_uuid=relationship.uuid,
            previous_values={"active": True},
            new_values={"active": False, "ended_reason": relationship.ended_reason},
        )
        return relationship


def revalidate_relationships_for_identity(*, patient, actor):
    """Re-evaluate parent links when a verified national card changes."""
    relationships = GuardianRelationship.objects.select_for_update(of=("self",)).filter(
        Q(minor_patient=patient) | Q(guardian_user__patient_profile=patient),
        verification_status__in=(
            GuardianRelationship.VerificationStatus.PENDING,
            GuardianRelationship.VerificationStatus.VERIFIED,
        ),
        ended_at__isnull=True,
    )
    for relationship in relationships:
        if (
            relationship.relationship
            == GuardianRelationship.Relationship.LEGAL_GUARDIAN
        ):
            continue
        evaluate_relationship_evidence(relationship)
        if (
            relationship.active
            and relationship.family_number_result
            != GuardianRelationship.FamilyNumberResult.MATCH
        ):
            relationship.active = False
            relationship.ended_at = timezone.now()
            relationship.ended_reason = (
                GuardianRelationship.EndedReason.RELATIONSHIP_INVALIDATED
            )
            relationship.ended_reason_detail = "Authoritative family evidence changed."
            relationship.save(
                update_fields=(
                    "active",
                    "ended_at",
                    "ended_reason",
                    "ended_reason_detail",
                    "updated_at",
                )
            )
            GuardianRelationshipEvent.objects.create(
                relationship=relationship,
                event_type=GuardianRelationshipEvent.EventType.ENDED,
                actor=actor,
                metadata={},
            )
            record_audit(
                action=AuditLog.Action.GUARDIAN_RELATIONSHIP_ENDED,
                actor=actor,
                patient=relationship.minor_patient,
                resource_type="GUARDIAN_RELATIONSHIP",
                resource_uuid=relationship.uuid,
                previous_values={"active": True},
                new_values={"active": False, "ended_reason": relationship.ended_reason},
            )


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


def authorized_guardian_relationship(user, minor, *, raise_ineligible=False):
    """Single medical and identity authorization policy for a minor link."""
    if (
        user.role != User.Role.PATIENT
        or user.status != User.Status.ACTIVE
        or not user.is_active
    ):
        return None
    filters = {
        "guardian_user": user,
        "verification_status": GuardianRelationship.VerificationStatus.VERIFIED,
        "active": True,
        "ended_at__isnull": True,
        "ended_reason": "",
    }
    if isinstance(minor, PatientProfile):
        filters["minor_patient"] = minor
    else:
        filters["minor_patient__uuid"] = minor
    try:
        relationship = (
            GuardianRelationship.objects.select_related("minor_patient")
            .filter(**filters)
            .first()
        )
    except (TypeError, ValueError):
        return None
    if relationship is None or not relationship.minor_patient.is_minor:
        return None
    try:
        eligible_guardian_profile(user)
    except GuardianNotVerified:
        if raise_ineligible:
            raise
        return None
    return relationship


def guardian_can_access_minor(user, minor):
    return authorized_guardian_relationship(user, minor) is not None
