from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from common.models import UUIDModel


class AuditLog(UUIDModel):
    class ActorType(models.TextChoices):
        USER = "USER", "User"
        SYSTEM = "SYSTEM", "System"

    class Action(models.TextChoices):
        # Account
        ACCOUNT_CREATED = "ACCOUNT_CREATED", "Account created"
        ACCOUNT_STATUS_CHANGED = "ACCOUNT_STATUS_CHANGED", "Account status changed"
        ACCOUNT_ACTIVATED = "ACCOUNT_ACTIVATED", "Account activated"
        ACCOUNT_CLAIM_SUBMITTED = "ACCOUNT_CLAIM_SUBMITTED", "Account claim submitted"
        ACCOUNT_CLAIM_APPROVED = "ACCOUNT_CLAIM_APPROVED", "Account claim approved"
        ACCOUNT_CLAIM_REJECTED = "ACCOUNT_CLAIM_REJECTED", "Account claim rejected"
        PATIENT_ACCOUNT_LINKED = "PATIENT_ACCOUNT_LINKED", "Patient account linked"
        PATIENT_PROFILE_CREATED = "PATIENT_PROFILE_CREATED", "Patient profile created"
        # Identity
        IDENTITY_DOCUMENT_UPLOADED = (
            "IDENTITY_DOCUMENT_UPLOADED",
            "Identity document uploaded",
        )
        IDENTITY_DOCUMENT_VERIFIED = (
            "IDENTITY_DOCUMENT_VERIFIED",
            "Identity document verified",
        )
        IDENTITY_DOCUMENT_REJECTED = (
            "IDENTITY_DOCUMENT_REJECTED",
            "Identity document rejected",
        )
        IDENTITY_DOCUMENT_REPLACED = (
            "IDENTITY_DOCUMENT_REPLACED",
            "Identity document replaced",
        )
        IDENTITY_REVIEW_FIELDS_CORRECTED = (
            "IDENTITY_REVIEW_FIELDS_CORRECTED",
            "Identity review fields corrected",
        )
        IDENTITY_VERIFIED_FIELDS_CORRECTED = (
            "IDENTITY_VERIFIED_FIELDS_CORRECTED",
            "Identity verified fields corrected",
        )
        PATIENT_IDENTITY_STATUS_CHANGED = (
            "PATIENT_IDENTITY_STATUS_CHANGED",
            "Patient identity status changed",
        )
        # Guardians
        MINOR_CREATED = "MINOR_CREATED", "Minor created"
        GUARDIAN_RELATIONSHIP_SUBMITTED = (
            "GUARDIAN_RELATIONSHIP_SUBMITTED",
            "Guardian relationship submitted",
        )
        GUARDIAN_RELATIONSHIP_VERIFIED = (
            "GUARDIAN_RELATIONSHIP_VERIFIED",
            "Guardian relationship verified",
        )
        GUARDIAN_RELATIONSHIP_REJECTED = (
            "GUARDIAN_RELATIONSHIP_REJECTED",
            "Guardian relationship rejected",
        )
        GUARDIAN_RELATIONSHIP_ENDED = (
            "GUARDIAN_RELATIONSHIP_ENDED",
            "Guardian relationship ended",
        )
        GUARDIAN_RELATIONSHIP_DISMISSED = (
            "GUARDIAN_RELATIONSHIP_DISMISSED",
            "Guardian relationship dismissed",
        )
        GUARDIAN_ACCESS_EXPIRED = (
            "GUARDIAN_ACCESS_EXPIRED",
            "Guardian access expired",
        )
        # Claims
        CLAIM_SUBMITTED = "CLAIM_SUBMITTED", "Claim submitted"
        CLAIM_MORE_INFORMATION_REQUIRED = (
            "CLAIM_MORE_INFORMATION_REQUIRED",
            "Claim more information required",
        )
        CLAIM_APPROVED = "CLAIM_APPROVED", "Claim approved"
        CLAIM_REJECTED = "CLAIM_REJECTED", "Claim rejected"
        ACCOUNT_ACTIVATION_CREATED = (
            "ACCOUNT_ACTIVATION_CREATED",
            "Account activation created",
        )
        # Medical documents
        DOCUMENT_UPLOADED = "DOCUMENT_UPLOADED", "Document uploaded"
        DOCUMENT_METADATA_UPDATED = (
            "DOCUMENT_METADATA_UPDATED",
            "Document metadata updated",
        )
        DOCUMENT_DELETED = "DOCUMENT_DELETED", "Document deleted"
        DOCUMENT_TYPE_CHANGED = "DOCUMENT_TYPE_CHANGED", "Document type changed"
        DOCUMENT_FACILITY_CHANGED = (
            "DOCUMENT_FACILITY_CHANGED",
            "Document facility changed",
        )
        FILE_INTEGRITY_CHECKED = (
            "FILE_INTEGRITY_CHECKED",
            "File integrity checked",
        )
        # Date authority
        DATE_CONFIRMED = "DATE_CONFIRMED", "Date confirmed"
        DATE_CORRECTED = "DATE_CORRECTED", "Date corrected"
        # Processing (security-significant only)
        PDF_EXTRACTION_FAILED = "PDF_EXTRACTION_FAILED", "PDF extraction failed"
        OCR_FAILED = "OCR_FAILED", "OCR failed"
        INTEGRITY_FAILURE = "INTEGRITY_FAILURE", "Integrity failure"

    SUPERUSER_ACCOUNT_PURGE_REQUESTED = "SUPERUSER_ACCOUNT_PURGE_REQUESTED"
    SUPERUSER_ACCOUNT_PURGE_COMPLETED = "SUPERUSER_ACCOUNT_PURGE_COMPLETED"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
        null=True,
        blank=True,
    )
    actor_type = models.CharField(
        max_length=16,
        choices=ActorType,
        default=ActorType.USER,
    )
    patient = models.ForeignKey(
        "patients.PatientProfile",
        on_delete=models.SET_NULL,
        related_name="audit_logs",
        null=True,
        blank=True,
    )
    resource_type = models.CharField(max_length=64, blank=True, default="")
    resource_uuid = models.UUIDField(null=True, blank=True)
    action = models.CharField(max_length=64, choices=Action)
    previous_values = models.JSONField(default=dict, blank=True)
    new_values = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    request_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ("-created_at", "-uuid")
        indexes = [
            models.Index(
                fields=("patient", "created_at"),
                name="audit_patient_created_idx",
            ),
            models.Index(
                fields=("actor", "created_at"),
                name="audit_actor_created_idx",
            ),
            models.Index(
                fields=("action", "created_at"),
                name="audit_action_created_idx",
            ),
            models.Index(
                fields=("resource_type", "resource_uuid"),
                name="audit_resource_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Audit records are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit records are immutable.")

    def __str__(self):
        return f"{self.action} {self.uuid}"
