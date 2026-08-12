from django.contrib import admin

from patients.models import PatientProfile


@admin.register(PatientProfile)
class PatientProfileAdmin(admin.ModelAdmin):
    list_display = (
        "digital_id",
        "full_name",
        "user",
        "date_of_birth",
        "identity_status",
        "created_at",
    )
    list_filter = ("identity_status", "sex", "blood_group")
    search_fields = ("digital_id", "full_name", "user__email")
    # Immutable identity keys + the service-driven identity_status are always
    # read-only. identity_status transitions happen only through the identity
    # verification service/API (which also writes events + audit + profile
    # state), never through the admin.
    readonly_fields = (
        "uuid",
        "digital_id",
        "identity_status",
        "created_at",
        "updated_at",
    )
    actions = None

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        """Lock identity-sensitive fields once the profile is VERIFIED.

        Admins must not casually re-edit verified identity data outside the
        verification workflow.
        """
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None and (
            obj.identity_status == PatientProfile.IdentityStatus.VERIFIED
        ):
            for name in ("full_name", "date_of_birth", "sex", "nationality"):
                if name not in fields:
                    fields.append(name)
        return fields
