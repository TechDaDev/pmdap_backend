"""Shared admin helpers.

The Django admin exists for operations/inspection. It must never be used to
bypass domain workflows (verification, claims review, guardian services,
processing pipelines). [ReadOnlyAdminMixin] makes an immutable / event-style
model inspectable but impossible to add, change or delete from the admin.
"""


class ReadOnlyAdminMixin:
    """Inspection-only admin for immutable/event models.

    Disables add, change and delete. Bulk actions are cleared as well.
    """

    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
