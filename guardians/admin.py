from django.contrib import admin

from common.admin import ReadOnlyAdminMixin
from guardians.models import (
    GuardianEvidence,
    GuardianRelationship,
    GuardianRelationshipEvent,
    MinorCreationRequest,
)


@admin.register(GuardianRelationship)
class GuardianRelationshipAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Read-only operational view.

    Transitions (verification_status/active) must not bypass the guardian
    services, so nothing here is editable from the admin.
    """

    list_display = (
        "uuid",
        "guardian_user",
        "minor_patient",
        "relationship",
        "verification_status",
        "active",
        "created_at",
    )
    list_filter = ("verification_status", "relationship", "active")
    search_fields = (
        "uuid",
        "guardian_user__email",
        "minor_patient__digital_id",
        "minor_patient__full_name",
    )
    readonly_fields = tuple(field.name for field in GuardianRelationship._meta.fields)


@admin.register(GuardianEvidence)
class GuardianEvidenceAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Metadata-only evidence view.

    No public file link: the underlying IdentityFile is metadata-only, and the
    `file` FK is excluded from the form.
    """

    list_display = ("uuid", "relationship", "evidence_type", "created_at")
    list_filter = ("evidence_type",)
    readonly_fields = ("uuid", "relationship", "evidence_type", "created_at", "updated_at")
    exclude = ("file",)


@admin.register(GuardianRelationshipEvent)
class GuardianRelationshipEventAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Read-only event journal."""

    list_display = ("uuid", "relationship", "event_type", "actor", "created_at")
    list_filter = ("event_type",)
    readonly_fields = tuple(
        field.name for field in GuardianRelationshipEvent._meta.fields
    )


@admin.register(MinorCreationRequest)
class MinorCreationRequestAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Read-only operational view of minor creation idempotency records."""

    list_display = ("uuid", "guardian_user", "minor_patient", "relationship", "created_at")
    readonly_fields = tuple(field.name for field in MinorCreationRequest._meta.fields)
