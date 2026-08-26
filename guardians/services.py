import hashlib
import json
import re
import unicodedata
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


@dataclass(frozen=True)
class GuardianApprovalEvaluation:
    eligible: bool
    code: str
    reasons: tuple[str, ...]
    adult_identity_verified: bool
    minor_identity_verified: bool
    age_valid: bool
    family_result: str
    family_explanation: str
    name_result: str
    name_explanation: str
    # "FATHER" / "MOTHER" / None — which minor name field the evidence uses.
    # Workstation must NOT show a father-name comparison for MOTHER.
    name_evidence_kind: str | None
    official_evidence_present: bool
    adult_card: object | None
    minor_card: object | None


EVIDENCE_POLICY_VERSION = "M29_3_V1"
_ARABIC_ALEF_TRANSLATION = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا"})
_ARABIC_DIACRITICS = re.compile(r"[\u0640\u064B-\u0652\u0670]")


def _name_evidence_source(relationship):
    """Return a minor-field accessor used for name evidence, or None.

    FATHER → minor.father_name (unchanged regression).
    MOTHER → minor.mother_name (authoritative maternal given name; father's
    name is NEVER substituted for maternal evidence).
    LEGAL_GUARDIAN → None (official evidence + manual review only).
    """
    if relationship.relationship == GuardianRelationship.Relationship.FATHER:
        return lambda minor: minor.father_name
    if relationship.relationship == GuardianRelationship.Relationship.MOTHER:
        return lambda minor: minor.mother_name
    return None


def normalize_family_number(value):
    return re.sub(r"\s+", "", (value or "").strip()).upper()


def _normalize_name(value):
    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = normalized.translate(_ARABIC_ALEF_TRANSLATION)
    normalized = _ARABIC_DIACRITICS.sub("", normalized)
    return " ".join(normalized.split()).casefold()


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
    name_source = _name_evidence_source(relationship)
    if (
        name_source is not None
        and guardian_card
        and minor_card
        and guardian_profile.identity_status == PatientProfile.IdentityStatus.VERIFIED
        and relationship.minor_patient.identity_status
        == PatientProfile.IdentityStatus.VERIFIED
    ):
        guardian_name = _normalize_name(guardian_profile.given_name)
        minor_name = _normalize_name(name_source(relationship.minor_patient))
        if guardian_name and minor_name:
            name_result = (
                GuardianRelationship.NameEvidenceResult.MATCH
                if guardian_name == minor_name
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


def can_approve_guardian_relationship(relationship, *, refresh=False):
    """Return the single fail-closed approval verdict used by UI and service."""
    evaluate_relationship_evidence(relationship, save=refresh)
    adult = relationship.guardian_user.patient_profile
    minor = relationship.minor_patient
    adult_card = relationship.guardian_identity_document
    minor_card = relationship.minor_identity_document
    adult_verified = bool(
        relationship.guardian_user.role == User.Role.PATIENT
        and relationship.guardian_user.status == User.Status.ACTIVE
        and relationship.guardian_user.is_active
        and not adult.is_minor
        and adult.identity_status == PatientProfile.IdentityStatus.VERIFIED
        and adult_card
    )
    minor_verified = bool(
        minor.identity_status == PatientProfile.IdentityStatus.VERIFIED and minor_card
    )
    age_valid = minor.is_minor
    official_evidence_present = relationship.evidences.exists()

    if not adult_card:
        family_explanation = "Unavailable — adult has no verified current National Card"
    elif not minor_card:
        family_explanation = "Unavailable — minor has no verified current National Card"
    elif not normalize_family_number(adult_card.family_number):
        family_explanation = (
            "Unavailable — adult verified card does not contain Family number"
        )
    elif not normalize_family_number(minor_card.family_number):
        family_explanation = (
            "Unavailable — Family number was not captured for the minor"
        )
    elif (
        relationship.family_number_result
        == GuardianRelationship.FamilyNumberResult.MATCH
    ):
        family_explanation = "Match — verified current National Cards"
    else:
        family_explanation = "Mismatch — verified current National Cards"

    name_explanation = "Not required for this relationship type"
    name_evidence_kind = None
    if relationship.relationship == GuardianRelationship.Relationship.FATHER:
        name_evidence_kind = "FATHER"
        if not adult_verified or not minor_verified:
            name_explanation = "Unavailable — identity is not verified"
        elif not _normalize_name(adult.given_name) or not _normalize_name(
            minor.father_name
        ):
            name_explanation = "Authoritative father/given-name data is unavailable"
        elif (
            relationship.name_evidence_result
            == GuardianRelationship.NameEvidenceResult.MATCH
        ):
            name_explanation = "Minor father's name matches adult given name"
        else:
            name_explanation = "Minor father's name does not match adult given name"
    elif relationship.relationship == GuardianRelationship.Relationship.MOTHER:
        name_evidence_kind = "MOTHER"
        if not adult_verified or not minor_verified:
            name_explanation = "Unavailable — identity is not verified"
        elif not _normalize_name(adult.given_name) or not _normalize_name(
            minor.mother_name
        ):
            name_explanation = (
                "Verified maternal-name evidence is unavailable."
            )
        elif (
            relationship.name_evidence_result
            == GuardianRelationship.NameEvidenceResult.MATCH
        ):
            name_explanation = "Minor mother's name matches adult given name"
        else:
            name_explanation = "Minor mother's name does not match adult given name"

    reasons = []
    if (
        relationship.verification_status
        != GuardianRelationship.VerificationStatus.PENDING
    ):
        reasons.append("Request is not pending.")
    if not adult_verified:
        reasons.append("Adult identity must be verified before approval.")
    if not minor_verified:
        reasons.append("Minor identity must be verified before approval.")
    if not age_valid:
        reasons.append("The patient must be under 18 at approval time.")
    if relationship.relationship in {
        GuardianRelationship.Relationship.FATHER,
        GuardianRelationship.Relationship.MOTHER,
    }:
        if (
            relationship.family_number_result
            != GuardianRelationship.FamilyNumberResult.MATCH
        ):
            reasons.append("Verified family evidence must match before approval.")
        if (
            relationship.relationship == GuardianRelationship.Relationship.FATHER
            and relationship.name_evidence_result
            != GuardianRelationship.NameEvidenceResult.MATCH
        ):
            reasons.append("Verified father-name evidence must match before approval.")
        if (
            relationship.relationship == GuardianRelationship.Relationship.MOTHER
            and relationship.name_evidence_result
            != GuardianRelationship.NameEvidenceResult.MATCH
        ):
            reasons.append("Verified mother-name evidence must match before approval.")
    elif not official_evidence_present:
        reasons.append("Official guardianship evidence is required before approval.")

    if not reasons:
        code = "ELIGIBLE"
    elif not minor_verified:
        code = "NOT_ELIGIBLE_MINOR_NOT_VERIFIED"
    elif not adult_verified:
        code = "NOT_ELIGIBLE_ADULT_NOT_VERIFIED"
    elif not age_valid:
        code = "NOT_ELIGIBLE_AGE"
    elif (
        relationship.relationship
        in {
            GuardianRelationship.Relationship.FATHER,
            GuardianRelationship.Relationship.MOTHER,
        }
        and relationship.family_number_result
        != GuardianRelationship.FamilyNumberResult.MATCH
    ):
        code = "NOT_ELIGIBLE_FAMILY_EVIDENCE"
    elif (
        relationship.relationship == GuardianRelationship.Relationship.FATHER
        and relationship.name_evidence_result
        != GuardianRelationship.NameEvidenceResult.MATCH
    ):
        code = "NOT_ELIGIBLE_FATHER_NAME_EVIDENCE"
    elif (
        relationship.relationship == GuardianRelationship.Relationship.MOTHER
        and relationship.name_evidence_result
        != GuardianRelationship.NameEvidenceResult.MATCH
    ):
        code = "NOT_ELIGIBLE_MOTHER_NAME_EVIDENCE"
    elif not official_evidence_present:
        code = "NOT_ELIGIBLE_OFFICIAL_EVIDENCE"
    else:
        code = "NOT_ELIGIBLE_REQUEST_STATE"

    return GuardianApprovalEvaluation(
        eligible=not reasons,
        code=code,
        reasons=tuple(reasons),
        adult_identity_verified=adult_verified,
        minor_identity_verified=minor_verified,
        age_valid=age_valid,
        family_result=relationship.family_number_result,
        family_explanation=family_explanation,
        name_result=relationship.name_evidence_result,
        name_explanation=name_explanation,
        name_evidence_kind=name_evidence_kind,
        official_evidence_present=official_evidence_present,
        adult_card=adult_card,
        minor_card=minor_card,
    )


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
        PatientProfile.objects.select_for_update().get(pk=relationship.minor_patient_id)
        decision = can_approve_guardian_relationship(relationship, refresh=True)
        if not decision.eligible:
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


def dismiss_guardian_relationship(*, relationship, guardian):
    """Patient-facing dismissal of a rejected/revoked relationship row.

    Pure presentation: hides the row from the default My Children list while
    preserving the immutable relationship, its events, and its audit history.
    The REJECTED/REVOKED status is never rewritten.

    Rules:
    - guardian must own the relationship (caller enforces ownership by query)
    - status REJECTED or REVOKED (ended) only; ACTIVE and PENDING → conflict
    - idempotent: dismissing an already-dismissed row is a no-op success
    """
    with transaction.atomic():
        relationship = (
            GuardianRelationship.objects.select_for_update()
            .select_related("minor_patient")
            .get(pk=relationship.pk)
        )
        if relationship.dismissed_by_guardian_at is not None:
            return relationship
        dismissible = (
            relationship.verification_status
            == GuardianRelationship.VerificationStatus.REJECTED
            or relationship.ended_at is not None
        )
        if not dismissible or relationship.active:
            raise GuardianRelationshipConflict()
        relationship.dismissed_by_guardian_at = timezone.now()
        relationship.save(
            update_fields=("dismissed_by_guardian_at", "updated_at")
        )
        GuardianRelationshipEvent.objects.create(
            relationship=relationship,
            event_type=GuardianRelationshipEvent.EventType.DISMISSED,
            actor=guardian,
            metadata={},
        )
        record_audit(
            action=AuditLog.Action.GUARDIAN_RELATIONSHIP_DISMISSED,
            actor=guardian,
            patient=relationship.minor_patient,
            resource_type="GUARDIAN_RELATIONSHIP",
            resource_uuid=relationship.uuid,
            previous_values={"dismissed_by_guardian_at": None},
            new_values={
                "dismissed_by_guardian_at": (
                    relationship.dismissed_by_guardian_at.isoformat()
                )
            },
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
