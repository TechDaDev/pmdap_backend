from django.contrib import admin

from common.admin import ReadOnlyAdminMixin
from claims.models import (
    AccountActivation,
    ClaimIdentityEvidence,
    PatientAccountClaim,
    PatientAccountClaimEvent,
)


@admin.register(PatientAccountClaim)
class PatientAccountClaimAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Search/filter/read-only operational view of account claims.

    Direct status editing that bypasses the claim review services is not
    possible here.
    """

    list_display = (
        "uuid",
        "patient",
        "requested_email",
        "status",
        "created_at",
        "reviewed_at",
    )
    list_filter = ("status",)
    search_fields = ("uuid", "patient__digital_id", "requested_email")
    readonly_fields = tuple(field.name for field in PatientAccountClaim._meta.fields)


@admin.register(ClaimIdentityEvidence)
class ClaimIdentityEvidenceAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Metadata-only evidence view.

    Identity images (IdentityFile FKs) are excluded from the form — no public
    identity image links are rendered.
    """

    list_display = (
        "uuid",
        "claim",
        "document_type",
        "issuing_country",
        "created_at",
    )
    list_filter = ("document_type",)
    readonly_fields = (
        "uuid",
        "claim",
        "document_type",
        "issuing_country",
        "issue_date",
        "expiry_date",
        "created_at",
        "updated_at",
    )
    exclude = ("document_number", "front_image", "back_image")


@admin.register(AccountActivation)
class AccountActivationAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Activation records.

    token_hash is NEVER rendered in the list and is shown in the detail form
    only as a redacted prefix.
    """

    list_display = ("uuid", "claim", "user", "expires_at", "used_at", "created_at")
    list_filter = ("expires_at",)
    readonly_fields = (
        "uuid",
        "claim",
        "user",
        "token_hash_redacted",
        "expires_at",
        "used_at",
        "created_at",
        "updated_at",
    )

    @admin.display(description="token_hash (redacted)")
    def token_hash_redacted(self, obj):
        if not obj.token_hash:
            return ""
        # Never leak the full hash in the admin UI.
        return f"{obj.token_hash[:8]}…{obj.token_hash[-4:]}"


@admin.register(PatientAccountClaimEvent)
class PatientAccountClaimEventAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Completely read-only claim event journal."""

    list_display = ("uuid", "claim", "event_type", "actor", "created_at")
    list_filter = ("event_type",)
    readonly_fields = tuple(
        field.name for field in PatientAccountClaimEvent._meta.fields
    )
