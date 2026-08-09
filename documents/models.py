from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from common.models import UUIDModel
from documents.storage import private_medical_storage
from patients.models import PatientProfile


class StoredFile(UUIDModel):
    class IntegrityStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        VALID = "VALID", "Valid"
        CORRUPTED = "CORRUPTED", "Corrupted"
        QUARANTINED = "QUARANTINED", "Quarantined"

    class MalwareScanStatus(models.TextChoices):
        NOT_CONFIGURED = "NOT_CONFIGURED", "Not configured"
        CLEAN = "CLEAN", "Clean"
        INFECTED = "INFECTED", "Infected"
        ERROR = "ERROR", "Error"

    IMMUTABLE_EVIDENCE_FIELDS = (
        "file",
        "original_filename",
        "mime_type",
        "size_bytes",
        "sha256",
        "page_count",
    )

    file = models.FileField(storage=private_medical_storage, max_length=500)
    original_filename = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=32)
    size_bytes = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    page_count = models.PositiveIntegerField(null=True, blank=True)
    integrity_status = models.CharField(
        max_length=16,
        choices=IntegrityStatus,
        default=IntegrityStatus.PENDING,
    )
    malware_scan_status = models.CharField(
        max_length=24,
        choices=MalwareScanStatus,
        default=MalwareScanStatus.NOT_CONFIGURED,
    )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = type(self).objects.get(pk=self.pk)
            changed = any(
                getattr(self, field).name != getattr(original, field).name
                if field == "file"
                else getattr(self, field) != getattr(original, field)
                for field in self.IMMUTABLE_EVIDENCE_FIELDS
            )
            if changed:
                raise ValidationError("Stored file evidence is immutable.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return str(self.uuid)


class MedicalDocument(UUIDModel):
    class DocumentType(models.TextChoices):
        LABORATORY = "LABORATORY", "Laboratory"
        RADIOLOGY = "RADIOLOGY", "Radiology"
        PRESCRIPTION = "PRESCRIPTION", "Prescription"
        CONSULTATION = "CONSULTATION", "Consultation"
        MEDICAL_REPORT = "MEDICAL_REPORT", "Medical report"
        HOSPITAL_ADMISSION = "HOSPITAL_ADMISSION", "Hospital admission"
        DISCHARGE_SUMMARY = "DISCHARGE_SUMMARY", "Discharge summary"
        SURGERY_PROCEDURE = "SURGERY_PROCEDURE", "Surgery procedure"
        PATHOLOGY = "PATHOLOGY", "Pathology"
        VACCINATION = "VACCINATION", "Vaccination"
        VITAL_SIGNS = "VITAL_SIGNS", "Vital signs"
        OTHER = "OTHER", "Other"

    class DateSource(models.TextChoices):
        USER_ENTERED = "USER_ENTERED", "User entered"
        PDF_TEXT = "PDF_TEXT", "PDF text"
        OCR = "OCR", "OCR"
        USER_CONFIRMED = "USER_CONFIRMED", "User confirmed"
        USER_CORRECTED = "USER_CORRECTED", "User corrected"

    class ClassificationSource(models.TextChoices):
        USER_SELECTED = "USER_SELECTED", "User selected"
        GUARDIAN_SELECTED = "GUARDIAN_SELECTED", "Guardian selected"
        SYSTEM_DEFAULT = "SYSTEM_DEFAULT", "System default"

    class ProcessingStatus(models.TextChoices):
        UPLOADED = "UPLOADED", "Uploaded"
        QUEUED = "QUEUED", "Queued"
        PROCESSING = "PROCESSING", "Processing"
        TEXT_EXTRACTED = "TEXT_EXTRACTED", "Text extracted"
        OCR_REQUIRED = "OCR_REQUIRED", "OCR required"
        OCR_PROCESSING = "OCR_PROCESSING", "OCR processing"
        DATE_PROCESSING = "DATE_PROCESSING", "Date processing"
        DATE_DETECTED = "DATE_DETECTED", "Date detected"
        DATE_NOT_FOUND = "DATE_NOT_FOUND", "Date not found"
        AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION", "Awaiting confirmation"
        DATE_CONFIRMED = "DATE_CONFIRMED", "Date confirmed"
        INDEXED = "INDEXED", "Indexed"
        FAILED = "FAILED", "Failed"

    class ArchiveStatus(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        DELETED = "DELETED", "Deleted"

    patient = models.ForeignKey(
        PatientProfile,
        on_delete=models.PROTECT,
        related_name="medical_documents",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_medical_documents",
    )
    stored_file = models.OneToOneField(
        StoredFile,
        on_delete=models.PROTECT,
        related_name="medical_document",
    )
    content_sha256 = models.CharField(max_length=64, editable=False)
    document_type = models.CharField(max_length=32, choices=DocumentType)
    classification_source = models.CharField(
        max_length=24,
        choices=ClassificationSource,
        default=ClassificationSource.SYSTEM_DEFAULT,
    )
    title = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    document_date = models.DateField(null=True, blank=True)
    date_source = models.CharField(
        max_length=24,
        choices=DateSource,
        blank=True,
        default="",
    )
    date_verified = models.BooleanField(default=False)
    date_verified_at = models.DateTimeField(null=True, blank=True)
    facility_name = models.CharField(max_length=255, blank=True)
    healthcare_facility = models.ForeignKey(
        "facilities.HealthcareFacility",
        on_delete=models.PROTECT,
        related_name="medical_documents",
        null=True,
        blank=True,
    )
    location_text = models.CharField(max_length=255, blank=True)
    department = models.CharField(max_length=255, blank=True)
    physician_name = models.CharField(max_length=255, blank=True)
    processing_status = models.CharField(
        max_length=24,
        choices=ProcessingStatus,
        default=ProcessingStatus.UPLOADED,
    )
    processing_failure_code = models.CharField(max_length=64, blank=True, default="")
    processing_started_at = models.DateTimeField(null=True, blank=True)
    archive_status = models.CharField(
        max_length=16,
        choices=ArchiveStatus,
        default=ArchiveStatus.ACTIVE,
    )
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="deleted_medical_documents",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ("-created_at", "-uuid")
        constraints = [
            models.UniqueConstraint(
                fields=("patient", "content_sha256"),
                condition=Q(archive_status="ACTIVE"),
                name="documents_active_patient_content_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=("patient", "archive_status", "document_date"),
                name="archive_status_date_idx",
            ),
            models.Index(
                fields=("patient", "archive_status", "document_type"),
                name="archive_status_type_idx",
            ),
            models.Index(
                fields=("patient", "archive_status", "healthcare_facility"),
                name="archive_status_facility_idx",
            ),
            models.Index(
                fields=("patient", "archive_status", "created_at"),
                name="archive_status_created_idx",
            ),
        ]

    def __str__(self):
        return str(self.uuid)


class MedicalDocumentEvent(UUIDModel):
    class EventType(models.TextChoices):
        UPLOADED = "MEDICAL_DOCUMENT_UPLOADED", "Uploaded"
        METADATA_UPDATED = (
            "MEDICAL_DOCUMENT_METADATA_UPDATED",
            "Metadata updated",
        )
        DOCUMENT_TYPE_CHANGED = "DOCUMENT_TYPE_CHANGED", "Document type changed"
        DOCUMENT_FACILITY_CHANGED = (
            "DOCUMENT_FACILITY_CHANGED",
            "Document facility changed",
        )
        DOCUMENT_LOCATION_UPDATED = (
            "DOCUMENT_LOCATION_UPDATED",
            "Document location updated",
        )
        DOCUMENT_DEPARTMENT_UPDATED = (
            "DOCUMENT_DEPARTMENT_UPDATED",
            "Document department updated",
        )
        DOCUMENT_PHYSICIAN_METADATA_UPDATED = (
            "DOCUMENT_PHYSICIAN_METADATA_UPDATED",
            "Document physician metadata updated",
        )
        DELETED = "MEDICAL_DOCUMENT_DELETED", "Deleted"
        DUPLICATE_REJECTED = (
            "MEDICAL_DOCUMENT_DUPLICATE_REJECTED",
            "Duplicate rejected",
        )
        FILE_INTEGRITY_CHECKED = (
            "MEDICAL_FILE_INTEGRITY_CHECKED",
            "File integrity checked",
        )
        PDF_EXTRACTION_QUEUED = "PDF_EXTRACTION_QUEUED", "PDF extraction queued"
        PDF_EXTRACTION_STARTED = (
            "PDF_EXTRACTION_STARTED",
            "PDF extraction started",
        )
        PDF_TEXT_EXTRACTED = "PDF_TEXT_EXTRACTED", "PDF text extracted"
        PDF_OCR_REQUIRED = "PDF_OCR_REQUIRED", "PDF OCR required"
        PDF_EXTRACTION_FAILED = "PDF_EXTRACTION_FAILED", "PDF extraction failed"
        OCR_QUEUED = "OCR_QUEUED", "OCR queued"
        OCR_STARTED = "OCR_STARTED", "OCR started"
        OCR_PAGE_COMPLETED = "OCR_PAGE_COMPLETED", "OCR page completed"
        OCR_COMPLETED = "OCR_COMPLETED", "OCR completed"
        OCR_FAILED = "OCR_FAILED", "OCR failed"
        DATE_PROCESSING_QUEUED = (
            "DATE_PROCESSING_QUEUED",
            "Date processing queued",
        )
        DATE_PROCESSING_STARTED = (
            "DATE_PROCESSING_STARTED",
            "Date processing started",
        )
        DATE_CANDIDATES_DETECTED = (
            "DATE_CANDIDATES_DETECTED",
            "Date candidates detected",
        )
        DATE_NOT_FOUND = "DATE_NOT_FOUND", "Date not found"
        DATE_PROCESSING_FAILED = (
            "DATE_PROCESSING_FAILED",
            "Date processing failed",
        )

    document = models.ForeignKey(
        MedicalDocument,
        on_delete=models.PROTECT,
        related_name="events",
    )
    event_type = models.CharField(max_length=48, choices=EventType)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="medical_document_events",
        null=True,
        blank=True,
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("created_at", "uuid")

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Medical document events are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Medical document events are immutable.")


class DocumentDateEvent(UUIDModel):
    class Action(models.TextChoices):
        DATE_CONFIRMED = "DATE_CONFIRMED", "Date confirmed"
        DATE_CORRECTED = "DATE_CORRECTED", "Date corrected"
        DATE_RECONFIRMED = "DATE_RECONFIRMED", "Date reconfirmed"

    document = models.ForeignKey(
        MedicalDocument,
        on_delete=models.PROTECT,
        related_name="date_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="medical_document_date_events",
    )
    action = models.CharField(max_length=24, choices=Action)
    previous_date = models.DateField(null=True, blank=True)
    new_date = models.DateField()
    source = models.CharField(max_length=24, choices=MedicalDocument.DateSource)
    candidate = models.ForeignKey(
        "processing.DateCandidate",
        on_delete=models.PROTECT,
        related_name="selection_events",
        null=True,
        blank=True,
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("created_at", "uuid")

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Document date events are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Document date events are immutable.")
