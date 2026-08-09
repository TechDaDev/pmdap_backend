from django.contrib import admin

from audit.models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "uuid",
        "action",
        "actor_type",
        "actor",
        "patient",
        "resource_type",
        "resource_uuid",
        "created_at",
    )
    list_filter = ("action", "actor_type", "resource_type")
    readonly_fields = [field.name for field in AuditLog._meta.fields]
    actions = []

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
