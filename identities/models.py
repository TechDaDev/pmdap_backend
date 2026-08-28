from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from common.models import UUIDModel
from identities.storage import private_identity_storage


class IdentityFile(UUIDModel):
    file = models.FileField(storage=private_identity_storage)
    original_name = models.CharField(max_length=255)
    media_type = models.CharField(max_length=16)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)

    def __str__(self):
        return str(self.uuid)


class IdentityDocument(UUIDModel):
    class DocumentType(models.TextChoices):
        UNIFIED_NATIONAL_CARD = (
            "UNIFIED_NATIONAL_CARD",
            "Unified National Card",
        )
        PASSPORT = "PASSPORT", "Passport"
        BIRTH_DOCUMENT = "BIRTH_DOCUMENT", "Birth document"
        OTHER_GOVERNMENT_ID = "OTHER_GOVERNMENT_ID", "Other government ID"

    class VerificationStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        VERIFIED = "VERIFIED", "Verified"
        REJECTED = "REJECTED", "Rejected"

    class LifecycleStatus(models.TextChoices):
        CURRENT = "CURRENT", "Current"
        EXPIRED = "EXPIRED", "Expired"
        REPLACED = "REPLACED", "Replaced"
        REVOKED = "REVOKED", "Revoked"

    patient = models.ForeignKey(
        "patients.PatientProfile",
        on_delete=models.PROTECT,
        related_name="identity_documents",
    )
    document_type = models.CharField(max_length=32, choices=DocumentType)
    document_number = models.CharField(max_length=128)
    national_number = models.CharField(max_length=128, blank=True)
    family_number = models.CharField(max_length=128, blank=True)
    # Short front-side H... code printed on the Iraqi National Card. A distinct
    # identifier from document_number / national_number / family_number; kept
    # in its own column so it survives to human verification untouched.
    unique_card_body_number = models.CharField(max_length=128, blank=True, default="")
    issuing_country = models.CharField(max_length=2)
    issue_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    # --- M29.5 reviewer-reviewed values (staged before approval) ---
    # Structured profile fields (names/DOB/sex/blood/nationality) live
    # authoritatively on PatientProfile; number fields live here. The reviewer
    # edits staged reviewed_* copies so authoritative values only change on
    # approval (or an explicit verified correction). An EMPTY value means "no
    # correction staged" (show the original); a non-empty value is staged.
    reviewed_given_name = models.CharField(max_length=255, blank=True, default="")
    reviewed_father_name = models.CharField(max_length=255, blank=True, default="")
    reviewed_grandfather_name = models.CharField(max_length=255, blank=True, default="")
    reviewed_mother_name = models.CharField(max_length=255, blank=True, default="")
    reviewed_date_of_birth = models.DateField(null=True, blank=True)
    reviewed_sex = models.CharField(max_length=16, blank=True, default="")
    reviewed_blood_group = models.CharField(max_length=7, blank=True, default="")
    reviewed_nationality = models.CharField(max_length=2, blank=True, default="")
    reviewed_document_number = models.CharField(max_length=128, blank=True, default="")
    reviewed_national_number = models.CharField(max_length=128, blank=True, default="")
    reviewed_family_number = models.CharField(max_length=128, blank=True, default="")
    reviewed_unique_card_body_number = models.CharField(
        max_length=128, blank=True, default=""
    )
    reviewed_issue_date = models.DateField(null=True, blank=True)
    reviewed_expiry_date = models.DateField(null=True, blank=True)
    # Optimistic-concurrency version for reviewed-value writes. Every
    # review-fields save increments it; clients must send the current version.
    review_version = models.PositiveIntegerField(default=0)
    front_image = models.ForeignKey(
        IdentityFile,
        on_delete=models.PROTECT,
        related_name="front_for_documents",
    )
    back_image = models.ForeignKey(
        IdentityFile,
        on_delete=models.PROTECT,
        related_name="back_for_documents",
        null=True,
        blank=True,
    )
    verification_status = models.CharField(
        max_length=16,
        choices=VerificationStatus,
        default=VerificationStatus.PENDING,
    )
    status = models.CharField(
        max_length=16,
        choices=LifecycleStatus,
        default=LifecycleStatus.CURRENT,
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="identity_documents_reviewed",
        null=True,
        blank=True,
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    replaces = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="replacement_attempts",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ("-created_at", "-uuid")
        constraints = [
            models.UniqueConstraint(
                fields=("patient", "document_type"),
                condition=Q(status="CURRENT", verification_status="VERIFIED"),
                name="identity_one_verified_current_type",
            ),
            models.CheckConstraint(
                condition=Q(expiry_date__isnull=True)
                | Q(issue_date__isnull=True)
                | Q(expiry_date__gt=models.F("issue_date")),
                name="identity_expiry_after_issue",
            ),
        ]
        indexes = [
            models.Index(
                fields=("verification_status", "created_at"),
                name="identity_verify_queue_idx",
            ),
            models.Index(
                fields=("patient", "document_type", "status"),
                name="identity_patient_type_idx",
            ),
        ]

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._original_uuid = instance.uuid
        return instance

    def save(self, *args, **kwargs):
        if not self._state.adding and self.uuid != getattr(
            self, "_original_uuid", self.uuid
        ):
            raise ValidationError("Identity document UUID is immutable.")
        result = super().save(*args, **kwargs)
        self._original_uuid = self.uuid
        return result

    def __str__(self):
        return f"{self.document_type}:{self.uuid}"


class IdentityDocumentEvent(UUIDModel):
    class EventType(models.TextChoices):
        UPLOADED = "IDENTITY_DOCUMENT_UPLOADED", "Identity document uploaded"
        REPLACEMENT_SUBMITTED = (
            "IDENTITY_DOCUMENT_REPLACEMENT_SUBMITTED",
            "Identity document replacement submitted",
        )
        VERIFIED = "IDENTITY_VERIFIED", "Identity verified"
        REJECTED = "IDENTITY_REJECTED", "Identity rejected"
        REPLACED = "IDENTITY_DOCUMENT_REPLACED", "Identity document replaced"
        REVIEW_FIELDS_CORRECTED = (
            "IDENTITY_REVIEW_FIELDS_CORRECTED",
            "Identity review fields corrected",
        )
        VERIFIED_FIELDS_CORRECTED = (
            "IDENTITY_VERIFIED_FIELDS_CORRECTED",
            "Identity verified fields corrected",
        )

    document = models.ForeignKey(
        IdentityDocument,
        on_delete=models.PROTECT,
        related_name="events",
    )
    event_type = models.CharField(max_length=48, choices=EventType)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="identity_document_events",
        null=True,
        blank=True,
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("created_at", "uuid")

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Identity document events are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Identity document events are immutable.")


class IdentityFieldCorrection(UUIDModel):
    """Immutable provenance record for a corrected identity field.

    Records the ORIGINAL authoritative value and the reviewed value per field
    so corrected values are never falsely labelled as OCR-extracted. Used for
    both pre-approval reviewer corrections and post-verification corrections.
    """

    class Source(models.TextChoices):
        REVIEWER_CORRECTION = "REVIEWER_CORRECTION", "Reviewer correction"
        VERIFIED_CORRECTION = "VERIFIED_CORRECTION", "Verified correction"

    class ReasonCategory(models.TextChoices):
        OCR_CORRECTION = (
            "OCR_CORRECTION",
            "OCR correction discovered after verification",
        )
        DATA_ENTRY = "DATA_ENTRY", "Data-entry correction"
        UPDATED_DOCUMENT = (
            "UPDATED_DOCUMENT",
            "Updated authoritative identity document",
        )
        ADMINISTRATIVE = "ADMINISTRATIVE", "Administrative correction"
        OTHER = "OTHER", "Other"

    document = models.ForeignKey(
        IdentityDocument,
        on_delete=models.PROTECT,
        related_name="field_corrections",
    )
    field = models.CharField(max_length=64)
    original_value = models.TextField(blank=True, default="")
    reviewed_value = models.TextField(blank=True, default="")
    source = models.CharField(
        max_length=32,
        choices=Source,
        default=Source.REVIEWER_CORRECTION,
    )
    review_version = models.PositiveIntegerField(default=0)
    corrected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="identity_field_corrections",
        null=True,
        blank=True,
    )
    corrected_at = models.DateTimeField(auto_now_add=True)
    reason_category = models.CharField(
        max_length=32,
        choices=ReasonCategory,
        blank=True,
        default="",
    )
    note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ("created_at", "uuid")
        indexes = [
            models.Index(fields=("document", "field"), name="idcorr_doc_field_idx")
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Identity field corrections are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Identity field corrections are immutable.")


class IdentityExtractionJob(UUIDModel):
    """Transient async extraction job.

    Holds status + S3/local staging keys only. Extracted field values are kept
    in the cache (TTL) so identity values are never persisted; the job row is
    deleted once the client consumes the result. Staging images are removed by
    the worker after processing.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        SUCCESS = "SUCCESS", "Success"
        FINALIZED = "FINALIZED", "Finalized"
        FAILED = "FAILED", "Failed"
        EXPIRED = "EXPIRED", "Expired"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="identity_extraction_jobs",
    )
    document_type = models.CharField(
        max_length=32,
        choices=IdentityDocument.DocumentType.choices,
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

    def __str__(self):
        return f"{self.uuid} {self.status}"
