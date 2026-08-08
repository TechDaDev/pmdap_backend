from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from common.models import UUIDModel


class PatientAccountClaim(UUIDModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        UNDER_REVIEW = "UNDER_REVIEW", "Under review"
        MORE_INFORMATION_REQUIRED = (
            "MORE_INFORMATION_REQUIRED",
            "More information required",
        )
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Cancelled"

    class Comparison(models.TextChoices):
        MATCH = "MATCH", "Match"
        MISMATCH = "MISMATCH", "Mismatch"
        UNAVAILABLE = "UNAVAILABLE", "Unavailable"

    ACTIVE_STATUSES = (
        Status.PENDING,
        Status.UNDER_REVIEW,
        Status.MORE_INFORMATION_REQUIRED,
    )

    patient = models.ForeignKey(
        "patients.PatientProfile",
        on_delete=models.PROTECT,
        related_name="account_claims",
    )
    requested_email = models.EmailField()
    requested_phone = models.CharField(max_length=32)
    submitted_name = models.CharField(max_length=255)
    submitted_date_of_birth = models.DateField()
    status = models.CharField(max_length=32, choices=Status, default=Status.PENDING)
    name_comparison = models.CharField(
        max_length=16, choices=Comparison, default=Comparison.UNAVAILABLE
    )
    date_of_birth_comparison = models.CharField(
        max_length=16, choices=Comparison, default=Comparison.UNAVAILABLE
    )
    document_number_comparison = models.CharField(
        max_length=16, choices=Comparison, default=Comparison.UNAVAILABLE
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="account_claims_reviewed",
        null=True,
        blank=True,
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    review_notes = models.TextField(blank=True)
    approved_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="approved_account_claim",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ("created_at", "uuid")
        constraints = [
            models.UniqueConstraint(
                fields=("patient",),
                condition=Q(
                    status__in=("PENDING", "UNDER_REVIEW", "MORE_INFORMATION_REQUIRED")
                ),
                name="claim_one_active_per_patient",
            ),
            models.UniqueConstraint(
                Lower("requested_email"),
                condition=Q(
                    status__in=("PENDING", "UNDER_REVIEW", "MORE_INFORMATION_REQUIRED")
                ),
                name="claim_one_active_per_email_ci",
            ),
        ]


class ClaimIdentityEvidence(UUIDModel):
    class DocumentType(models.TextChoices):
        UNIFIED_NATIONAL_CARD = "UNIFIED_NATIONAL_CARD", "Unified National Card"
        PASSPORT = "PASSPORT", "Passport"

    claim = models.ForeignKey(
        PatientAccountClaim, on_delete=models.PROTECT, related_name="identity_evidence"
    )
    document_type = models.CharField(max_length=32, choices=DocumentType)
    document_number = models.CharField(max_length=128)
    issuing_country = models.CharField(max_length=2, default="IQ")
    issue_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    front_image = models.ForeignKey(
        "identities.IdentityFile",
        on_delete=models.PROTECT,
        related_name="claim_front_evidence",
    )
    back_image = models.ForeignKey(
        "identities.IdentityFile",
        on_delete=models.PROTECT,
        related_name="claim_back_evidence",
        null=True,
        blank=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("claim", "document_type"), name="claim_one_evidence_type"
            )
        ]


class AccountActivation(UUIDModel):
    claim = models.OneToOneField(
        PatientAccountClaim, on_delete=models.PROTECT, related_name="activation"
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="claim_activation",
    )
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)


class PatientAccountClaimEvent(UUIDModel):
    class EventType(models.TextChoices):
        SUBMITTED = "CLAIM_SUBMITTED", "Claim submitted"
        UNDER_REVIEW = "CLAIM_UNDER_REVIEW", "Claim under review"
        MORE_INFORMATION_REQUIRED = (
            "CLAIM_MORE_INFORMATION_REQUIRED",
            "More information required",
        )
        APPROVED = "CLAIM_APPROVED", "Claim approved"
        PATIENT_LINKED = "PATIENT_ACCOUNT_LINKED", "Patient account linked"
        ACTIVATION_CREATED = "ACCOUNT_ACTIVATION_CREATED", "Activation created"
        REJECTED = "CLAIM_REJECTED", "Claim rejected"
        ACTIVATED = "CLAIM_ACCOUNT_ACTIVATED", "Account activated"

    claim = models.ForeignKey(
        PatientAccountClaim, on_delete=models.PROTECT, related_name="events"
    )
    event_type = models.CharField(max_length=48, choices=EventType)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="account_claim_events",
        null=True,
        blank=True,
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("created_at", "uuid")

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Account claim events are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Account claim events are immutable.")
