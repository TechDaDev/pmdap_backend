"""Shared helpers for building Django admin template context."""


def admin_context(request, **extra):
    """Return the default admin context (site header/title/perms) plus extras.

    ``django.contrib.admin.sites.site`` is replaced at import time by
    ``opsconsole.admin``, so this resolves to the PMDAP operations site.
    """
    from django.contrib.admin.sites import site as admin_site

    context = admin_site.each_context(request)
    context.update(extra)
    return context
