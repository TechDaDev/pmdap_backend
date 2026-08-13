"""PMDAP Operations admin site.

Replaces the default Django admin site with an ``AdminSite`` subclass that adds
the operations console pages:

  * /admin/identity-verification/              - verification queue
  * /admin/identity-verification/<uuid>/       - verification review
  * /admin/identity-verification/<uuid>/approve/ - approve (POST) + confirm (GET)
  * /admin/identity-verification/<uuid>/reject/  - reject (POST) + form (GET)
  * /admin/operations/identity/<uuid>/image/front|back/ - private image stream
  * /admin/operations/server-monitor/          - server monitor dashboard
  * /admin/operations/server-monitor/data/     - monitor JSON (Redis only)

The replacement must happen before every other app's ``admin.py`` is imported
during autodiscovery, so this app is listed first in ``PROJECT_APPS``.
"""

from django.contrib.admin import AdminSite, sites as admin_sites
from django.urls import path
import django.contrib.admin as admin_mod

from opsconsole import monitor_views
from opsconsole import verification_views


class OpsAdminSite(AdminSite):
    site_header = "PMDAP Operations"
    site_title = "PMDAP Operations"
    index_title = "PMDAP Operations Console"
    index_template = "admin/ops/index.html"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "identity-verification/",
                self.admin_view(verification_views.verification_queue),
                name="ops_verification_queue",
            ),
            path(
                "identity-verification/<uuid:document_uuid>/",
                self.admin_view(verification_views.verification_review),
                name="ops_verification_review",
            ),
            path(
                "identity-verification/<uuid:document_uuid>/approve/",
                self.admin_view(verification_views.verification_approve),
                name="ops_verification_approve",
            ),
            path(
                "identity-verification/<uuid:document_uuid>/reject/",
                self.admin_view(verification_views.verification_reject),
                name="ops_verification_reject",
            ),
            path(
                "operations/identity/<uuid:document_uuid>/image/front/",
                self.admin_view(verification_views.identity_image_front),
                name="ops_identity_image_front",
            ),
            path(
                "operations/identity/<uuid:document_uuid>/image/back/",
                self.admin_view(verification_views.identity_image_back),
                name="ops_identity_image_back",
            ),
            path(
                "operations/server-monitor/",
                self.admin_view(monitor_views.server_monitor),
                name="ops_server_monitor",
            ),
            path(
                "operations/server-monitor/data/",
                self.admin_view(monitor_views.server_monitor_data),
                name="ops_server_monitor_data",
            ),
        ]
        return custom + urls

    def index(self, request, extra_context=None):
        """Add operations-console cards to the admin dashboard."""
        from identities.models import IdentityDocument

        context = {
            "ops_pending_count": IdentityDocument.objects.filter(
                verification_status=IdentityDocument.VerificationStatus.PENDING,
                status=IdentityDocument.LifecycleStatus.CURRENT,
            ).count(),
            "ops_monitor_url": "admin:ops_server_monitor",
            "ops_verification_url": "admin:ops_verification_queue",
        }
        if extra_context:
            context.update(extra_context)
        return super().index(request, context)


ops_site = OpsAdminSite(name="admin")

# Both namespaces must point at the replacement site so that:
#   * @admin.register(Model) (no site=...) uses it via django.contrib.admin.sites.site
#   * admin.site.urls / admin.site.index in config/urls.py use it via the package attr
admin_sites.site = ops_site
admin_mod.site = ops_site
