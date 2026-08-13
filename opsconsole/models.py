"""Models for the operations console.

No tables are created by this app. The only model below exists so that the
``opsconsole.view_server_monitor`` permission is registered with Django and can
be granted to staff accounts in the admin user editor.
"""

from django.db import models


class ServerMonitorPermission(models.Model):
    """Managed=False model used purely to register the server monitor permission."""

    class Meta:
        managed = False
        default_permissions = ()
        app_label = "opsconsole"
        permissions = [
            ("view_server_monitor", "Can view the Railway server monitor"),
        ]
