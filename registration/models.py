"""Short-lived, capability-bound pre-registration identity sessions.

A public (anonymous) client uploads the Iraqi National Card once, the OCR
worker extracts advisory fields, and the user reviews/corrects them before
completing registration. Ownership is a capability token: only a SHA-256
digest is stored server-side and the token is returned to the client exactly
once. The job never holds email/phone/password or extracted values.
"""
from django.db import models

from common.models import UUIDModel
from identities.models import IdentityDocument


class RegistrationSession(UUIDModel):
    """Capability-bound pre-registration session requiring email verification.

    Created from account details before identity OCR. Ownership is a high-
    entropy ``session_token``: only its SHA-256 digest is stored and the token
    is returned to the client exactly once. The normalized email is stored
    because it is required to deliver the email-verification OTP and to bind
    the final registration; API responses only ever expose a masked form. The
    password is never stored here — it remains client-side until the final
    register. State is authoritative and durable so verification survives app
    refresh/restart.
    """

    class Status(models.TextChoices):
        PENDING_EMAIL_VERIFICATION = (
            "PENDING_EMAIL_VERIFICATION",
            "Pending email verification",
        )
        EMAIL_VERIFIED = "EMAIL_VERIFIED", "Email verified"
        FINALIZED = "FINALIZED", "Finalized"
        EXPIRED = "EXPIRED", "Expired"

    capability_digest = models.CharField(max_length=64, db_index=True)
    email = models.EmailField()
    phone = models.CharField(max_length=32, blank=True, default="")
    governorate = models.CharField(max_length=32, blank=True, default="")
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.PENDING_EMAIL_VERIFICATION,
        db_index=True,
    )
    email_verified_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    finalized_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("status", "expires_at"),
                name="reg_session_status_expiry_idx",
            )
        ]

    def __str__(self):
        return f"{self.uuid} {self.status}"


class RegistrationIdentityExtractionJob(UUIDModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        SUCCESS = "SUCCESS", "Success"
        FINALIZED = "FINALIZED", "Finalized"
        FAILED = "FAILED", "Failed"
        EXPIRED = "EXPIRED", "Expired"

    # SHA-256 hex digest of the high-entropy job_token (capability). The raw
    # token is never stored and is returned to the client only at creation.
    capability_digest = models.CharField(max_length=64)
    # The email-verified registration session this job was created under
    # (M31B). A job may only be issued for an EMAIL_VERIFIED session.
    session = models.ForeignKey(
        RegistrationSession,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="extraction_jobs",
    )
    document_type = models.CharField(
        max_length=32,
        choices=IdentityDocument.DocumentType.choices,
        default=IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    front_key = models.CharField(max_length=512, blank=True, default="")
    back_key = models.CharField(max_length=512, blank=True, default="")
    error_code = models.CharField(max_length=64, blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("status", "expires_at"),
                name="reg_job_status_expiry_idx",
            )
        ]

    def __str__(self):
        return f"{self.uuid} {self.status}"
