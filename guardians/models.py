from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from common.models import UUIDModel


class GuardianRelationship(UUIDModel):
    class Relationship(models.TextChoices):
        FATHER = "FATHER", "Father"
        MOTHER = "MOTHER", "Mother"
        LEGAL_GUARDIAN = "LEGAL_GUARDIAN", "Legal guardian"

    class VerificationStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        VERIFIED = "VERIFIED", "Verified"
        REJECTED = "REJECTED", "Rejected"

    class FamilyNumberResult(models.TextChoices):
        MATCH = "MATCH", "Match"
        MISMATCH = "MISMATCH", "Mismatch"
        UNAVAILABLE = "UNAVAILABLE", "Unavailable"

    class NameEvidenceResult(models.TextChoices):
        MATCH = "MATCH", "Match"
        MISMATCH = "MISMATCH", "Mismatch"
        UNAVAILABLE = "UNAVAILABLE", "Unavailable"

    class EndedReason(models.TextChoices):
        PATIENT_REACHED_ADULTHOOD = (
            "PATIENT_REACHED_ADULTHOOD",
            "Patient reached adulthood",
        )
        REVOKED = "REVOKED", "Revoked"
        RELATIONSHIP_INVALIDATED = (
            "RELATIONSHIP_INVALIDATED",
            "Relationship invalidated",
        )
        ADMINISTRATIVE_CORRECTION = (
            "ADMINISTRATIVE_CORRECTION",
            "Administrative correction",
        )
        OTHER = "OTHER", "Other"

    guardian_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="guardian_relationships",
    )
    minor_patient = models.ForeignKey(
        "patients.PatientProfile",
        on_delete=models.PROTECT,
        related_name="guardian_relationships",
    )
    relationship = models.CharField(max_length=24, choices=Relationship)
    verification_status = models.CharField(
        max_length=16, choices=VerificationStatus, default=VerificationStatus.PENDING
    )
    family_number_result = models.CharField(
        max_length=16,
        choices=FamilyNumberResult,
        default=FamilyNumberResult.UNAVAILABLE,
    )
    name_evidence_result = models.CharField(
        max_length=16,
        choices=NameEvidenceResult,
        default=NameEvidenceResult.UNAVAILABLE,
    )
    guardian_identity_document = models.ForeignKey(
        "identities.IdentityDocument",
        on_delete=models.PROTECT,
        related_name="guardian_relationship_checks",
        null=True,
        blank=True,
    )
    minor_identity_document = models.ForeignKey(
        "identities.IdentityDocument",
        on_delete=models.PROTECT,
        related_name="minor_relationship_checks",
        null=True,
        blank=True,
    )
    evidence_checked_at = models.DateTimeField(null=True, blank=True)
    evidence_policy_version = models.CharField(max_length=32, blank=True, default="")
    active = models.BooleanField(default=False)
    started_at = models.DateTimeField(default=timezone.now)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="guardian_relationships_reviewed",
        null=True,
        blank=True,
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_reason = models.CharField(max_length=32, choices=EndedReason, blank=True)
    ended_reason_detail = models.TextField(blank=True)
    rejection_reason = models.TextField(blank=True)

    class Meta:
        ordering = ("-created_at", "-uuid")
        constraints = [
            models.UniqueConstraint(
                fields=("guardian_user", "minor_patient", "relationship"),
                condition=Q(
                    verification_status__in=("PENDING", "VERIFIED"),
                    ended_at__isnull=True,
                ),
                name="guardian_one_live_relationship_type",
            ),
            models.CheckConstraint(
                condition=Q(active=False) | Q(verification_status="VERIFIED"),
                name="guardian_active_requires_verified",
            ),
        ]
        indexes = [
            models.Index(
                fields=("verification_status", "created_at"),
                name="guardian_verify_queue_idx",
            ),
            models.Index(
                fields=("guardian_user", "active"), name="guardian_user_active_idx"
            ),
        ]


class GuardianEvidence(UUIDModel):
    class EvidenceType(models.TextChoices):
        LEGAL_GUARDIANSHIP_DOCUMENT = (
            "LEGAL_GUARDIANSHIP_DOCUMENT",
            "Legal guardianship document",
        )
        COURT_DOCUMENT = "COURT_DOCUMENT", "Court document"
        OTHER_OFFICIAL_EVIDENCE = (
            "OTHER_OFFICIAL_EVIDENCE",
            "Other official evidence",
        )

    relationship = models.ForeignKey(
        GuardianRelationship, on_delete=models.PROTECT, related_name="evidences"
    )
    evidence_type = models.CharField(max_length=32, choices=EvidenceType)
    file = models.ForeignKey(
        "identities.IdentityFile",
        on_delete=models.PROTECT,
        related_name="guardian_evidences",
    )
    metadata = models.JSONField(default=dict, blank=True)


class GuardianRelationshipEvent(UUIDModel):
    class EventType(models.TextChoices):
        MINOR_CREATED = "MINOR_CREATED", "Minor created"
        SUBMITTED = "GUARDIAN_RELATIONSHIP_SUBMITTED", "Relationship submitted"
        DOCUMENT_SUBMITTED = (
            "MINOR_IDENTITY_DOCUMENT_SUBMITTED",
            "Minor identity document submitted",
        )
        FAMILY_MATCHED = "FAMILY_NUMBER_MATCHED", "Family number matched"
        FAMILY_MISMATCHED = "FAMILY_NUMBER_MISMATCHED", "Family number mismatched"
        VERIFIED = "GUARDIAN_RELATIONSHIP_VERIFIED", "Relationship verified"
        REJECTED = "GUARDIAN_RELATIONSHIP_REJECTED", "Relationship rejected"
        ENDED = "GUARDIAN_RELATIONSHIP_ENDED", "Relationship ended"

    relationship = models.ForeignKey(
        GuardianRelationship, on_delete=models.PROTECT, related_name="events"
    )
    event_type = models.CharField(max_length=48, choices=EventType)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="guardian_relationship_events",
        null=True,
        blank=True,
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("created_at", "uuid")

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Guardian relationship events are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Guardian relationship events are immutable.")


class MinorCreationRequest(UUIDModel):
    guardian_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="minor_creation_requests",
    )
    idempotency_key = models.CharField(max_length=128)
    request_hash = models.CharField(max_length=64)
    minor_patient = models.OneToOneField(
        "patients.PatientProfile",
        on_delete=models.PROTECT,
        related_name="creation_request",
        null=True,
        blank=True,
    )
    relationship = models.OneToOneField(
        GuardianRelationship,
        on_delete=models.PROTECT,
        related_name="creation_request",
        null=True,
        blank=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("guardian_user", "idempotency_key"),
                name="guardian_minor_create_idempotency",
            )
        ]
