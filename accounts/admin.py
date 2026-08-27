from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.admin import UserAdmin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from accounts.models import User
from accounts.purge import (
    PURGE_REASONS,
    AccountPurgeBlocked,
    can_system_purge_users,
    preview_user_purge,
    purge_user_account_as_superuser,
)


class SystemPurgeForm(forms.Form):
    reason = forms.ChoiceField(choices=PURGE_REASONS)
    reason_detail = forms.CharField(required=False, max_length=200)
    confirm = forms.BooleanField(
        label="I understand this cannot be undone",
        required=True,
    )


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    actions = ("system_purge_selected_users",)
    change_form_template = "admin/accounts/user/change_form.html"
    ordering = ("email",)
    list_display = ("email", "role", "status", "is_active", "is_staff")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (
            "Account",
            {
                "fields": (
                    "phone",
                    "role",
                    "status",
                    "email_verified",
                    "phone_verified",
                )
            },
        ),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        (
            "Dates",
            {"fields": ("last_login", "date_joined", "created_at", "updated_at")},
        ),
    )
    readonly_fields = ("created_at", "updated_at")
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2", "role", "status"),
            },
        ),
    )

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        if not can_system_purge_users(request.user):
            actions.pop("system_purge_selected_users", None)
        return actions

    def get_urls(self):
        custom = [
            path(
                "<path:object_id>/system-purge/",
                self.admin_site.admin_view(self.system_purge_view),
                name="accounts_user_system_purge",
            )
        ]
        return custom + super().get_urls()

    @admin.action(description="System purge selected users")
    def system_purge_selected_users(self, request, queryset):
        self._require_system_purge(request)
        selected = request.POST.getlist(ACTION_CHECKBOX_NAME)
        if request.POST.get("confirm_purge") == "yes":
            form = SystemPurgeForm(request.POST)
            if form.is_valid():
                self._purge_each(request, queryset, form.cleaned_data)
                return HttpResponseRedirect(reverse("admin:accounts_user_changelist"))
        else:
            form = SystemPurgeForm()
        return self._confirmation_response(request, queryset, form, selected)

    def system_purge_view(self, request, object_id):
        self._require_system_purge(request)
        target = self.get_object(request, object_id)
        if target is None:
            return HttpResponseRedirect(reverse("admin:accounts_user_changelist"))
        queryset = User.objects.filter(pk=target.pk)
        if request.method == "POST":
            form = SystemPurgeForm(request.POST)
            if form.is_valid():
                self._purge_each(request, queryset, form.cleaned_data)
                return HttpResponseRedirect(reverse("admin:accounts_user_changelist"))
        else:
            form = SystemPurgeForm()
        return self._confirmation_response(request, queryset, form, [str(target.pk)])

    def _require_system_purge(self, request):
        if not can_system_purge_users(request.user):
            raise PermissionDenied("System purge requires an active superuser.")

    def _confirmation_response(self, request, queryset, form, selected):
        targets = list(queryset.order_by("uuid"))
        previews = [
            preview_user_purge(actor=request.user, target=user) for user in targets
        ]
        context = {
            **self.admin_site.each_context(request),
            "title": "Confirm system purge",
            "opts": self.model._meta,
            "form": form,
            "previews": previews,
            "selected": selected,
            "action_checkbox_name": ACTION_CHECKBOX_NAME,
            "is_bulk": len(targets) != 1
            or request.resolver_match.url_name == "accounts_user_changelist",
        }
        return TemplateResponse(
            request, "admin/accounts/user/system_purge_confirmation.html", context
        )

    def _purge_each(self, request, queryset, cleaned_data):
        for target in list(queryset.order_by("uuid")):
            target_uuid = str(target.pk)
            try:
                purge_user_account_as_superuser(
                    actor=request.user,
                    target=target,
                    reason=cleaned_data["reason"],
                    reason_detail=cleaned_data["reason_detail"],
                )
            except AccountPurgeBlocked as exc:
                self.message_user(
                    request,
                    f"BLOCKED {target_uuid}: {exc}",
                    level=messages.WARNING,
                )
            except Exception:
                self.message_user(
                    request,
                    f"FAILED {target_uuid}: purge rolled back",
                    level=messages.ERROR,
                )
            else:
                self.message_user(
                    request, f"SUCCESS {target_uuid}", level=messages.SUCCESS
                )
