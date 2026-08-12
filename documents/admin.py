from django.contrib import admin

from common.admin import ReadOnlyAdminMixin
from documents.models import (
    DocumentDateEvent,
    MedicalDocument,
    MedicalDocumentEvent,
    StoredFile,
)


@admin.register(StoredFile)
class StoredFileAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Metadata-only read-only view of stored medical files.

    The FileField is excluded — no clickable public URL is rendered.
    """

    list_display = (
        "uuid",
        "original_filename",
        "mime_type",
        "size_bytes",
        "sha256",
        "integrity_status",
        "created_at",
    )
    list_filter = ("mime_type", "integrity_status", "malware_scan_status")
    readonly_fields = (
        "uuid",
        "original_filename",
        "mime_type",
        "size_bytes",
        "sha256",
        "page_count",
        "integrity_status",
        "malware_scan_status",
        "created_at",
        "updated_at",
    )
    exclude = ("file",)


@admin.register(MedicalDocument)
class MedicalDocumentAdmin(admin.ModelAdmin):
    """Operational metadata view.

    Extracted medical text is not stored here and is never rendered.
    Pipeline-driven fields (processing_status, archive_status, date
    verification, immutable evidence) are read-only so admin cannot bypass the
    extraction/date/archive domain flows.
    """

    list_display = (
        "uuid",
        "patient",
        "document_type",
        "processing_status",
        "archive_status",
        "document_date",
        "date_verified",
        "created_at",
    )
    list_filter = (
        "document_type",
        "processing_status",
        "archive_status",
        "date_verified",
    )
    search_fields = ("uuid", "patient__digital_id", "title")
    readonly_fields = (
        "uuid",
        "patient",
        "uploaded_by",
        "stored_file",
        "content_sha256",
        "document_type",
        "processing_status",
        "processing_failure_code",
        "processing_started_at",
        "archive_status",
        "deleted_at",
        "deleted_by",
        "document_date",
        "date_source",
        "date_verified",
        "date_verified_at",
        "created_at",
        "updated_at",
    )
    # Metadata corrections ops may legitimately perform without touching the
    # extraction/date/archive pipelines.
    fields = (
        "uuid",
        "patient",
        "uploaded_by",
        "stored_file",
        "content_sha256",
        "document_type",
        "title",
        "description",
        "classification_source",
        "facility_name",
        "healthcare_facility",
        "location_text",
        "department",
        "physician_name",
        "document_date",
        "date_source",
        "date_verified",
        "date_verified_at",
        "processing_status",
        "processing_failure_code",
        "processing_started_at",
        "archive_status",
        "deleted_at",
        "deleted_by",
        "created_at",
        "updated_at",
    )
    actions = None

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MedicalDocumentEvent)
class MedicalDocumentEventAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Completely read-only medical document event journal."""

    list_display = ("uuid", "document", "event_type", "actor", "created_at")
    list_filter = ("event_type",)
    readonly_fields = tuple(field.name for field in MedicalDocumentEvent._meta.fields)


@admin.register(DocumentDateEvent)
class DocumentDateEventAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Completely read-only document date event journal."""

    list_display = (
        "uuid",
        "document",
        "action",
        "actor",
        "previous_date",
        "new_date",
        "created_at",
    )
    list_filter = ("action",)
    readonly_fields = tuple(field.name for field in DocumentDateEvent._meta.fields)
