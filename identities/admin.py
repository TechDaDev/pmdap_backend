from django.contrib import admin

from common.admin import ReadOnlyAdminMixin
from identities.models import (
    IdentityDocument,
    IdentityDocumentEvent,
    IdentityExtractionJob,
    IdentityFieldCorrection,
    IdentityFile,
)

# Sensitive document numbers (document_number, national_number, family_number)
# are intentionally NOT in any list_display. They remain searchable by a
# privileged superuser via search_fields, but are never rendered across the
# whole list page.


@admin.register(IdentityFile)
class IdentityFileAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Metadata-only view of stored identity images.

    The underlying FileField is excluded so no clickable public file URL is
    rendered. No replacement/edit/delete.
    """

    list_display = ("uuid", "original_name", "media_type", "size", "sha256", "created_at")
    list_filter = ("media_type",)
    readonly_fields = (
        "uuid",
        "original_name",
        "media_type",
        "size",
        "sha256",
        "created_at",
        "updated_at",
    )
    # Never render the FileField (no file link), never offer edits.
    exclude = ("file",)


@admin.register(IdentityDocument)
class IdentityDocumentAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Effectively read-only operational view of identity documents.

    Verification transitions must still happen through the verification
    service/API so events, audit and the profile identity status stay correct.
    Admin can never edit verification_status/status directly.
    """

    list_display = (
        "uuid",
        "patient",
        "document_type",
        "verification_status",
        "status",
        "issuing_country",
        "created_at",
        "verified_at",
    )
    list_filter = (
        "document_type",
        "verification_status",
        "status",
        "issuing_country",
    )
    search_fields = (
        "uuid",
        "patient__digital_id",
        "patient__full_name",
        "document_number",
    )
    readonly_fields = tuple(field.name for field in IdentityDocument._meta.fields)
    # front_image/back_image are FK links to the metadata-only IdentityFile
    # admin (no public URL). Keep them out of list_display regardless.
    list_per_page = 50


@admin.register(IdentityDocumentEvent)
class IdentityDocumentEventAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Completely read-only event journal. No sensitive metadata rendered."""

    list_display = ("uuid", "document", "event_type", "actor", "created_at")
    list_filter = ("event_type",)
    readonly_fields = tuple(field.name for field in IdentityDocumentEvent._meta.fields)
    list_per_page = 100


@admin.register(IdentityFieldCorrection)
class IdentityFieldCorrectionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Read-only provenance journal for identity corrections.

    Immutable by design; corrections only happen through the workstation /
    domain service. Value columns are excluded from the list page.
    """

    list_display = (
        "uuid",
        "document",
        "field",
        "source",
        "corrected_by",
        "corrected_at",
        "reason_category",
    )
    list_filter = ("source", "reason_category", "field")
    readonly_fields = tuple(
        field.name for field in IdentityFieldCorrection._meta.fields
    )
    list_per_page = 100


@admin.register(IdentityExtractionJob)
class IdentityExtractionJobAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Operational read-only diagnostic view of extraction jobs.

    Storage keys (front_key/back_key) are never rendered — not in the list,
    not in the detail form. No S3 links are ever provided.
    """

    list_display = (
        "uuid",
        "user",
        "document_type",
        "status",
        "error_code",
        "created_at",
        "updated_at",
    )
    list_filter = ("status", "document_type")
    fields = (
        "uuid",
        "user",
        "document_type",
        "status",
        "error_code",
        "created_at",
        "updated_at",
    )
    list_per_page = 50
