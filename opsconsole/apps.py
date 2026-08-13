from django.apps import AppConfig


class OpsConsoleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "opsconsole"
    verbose_name = "PMDAP Operations"

    def ready(self):
        # Import the collector module so the Celery task is registered, and
        # bootstrap the self-rescheduling Railway metrics collector chain.
        from opsconsole import collector  # noqa: F401

        collector.ensure_collector()
