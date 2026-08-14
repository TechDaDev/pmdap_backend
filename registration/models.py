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
