"""Sweep expired/abandoned identity extraction jobs.

Removes private staging images, cached results and job rows for jobs older
than IDENTITY_STAGING_TTL_SECONDS that were never finalized. Run as:
  python manage.py cleanup_identity_extraction_jobs
"""
from django.core.management.base import BaseCommand

from identities.tasks import cleanup_identity_extraction_jobs


class Command(BaseCommand):
    help = "Remove expired identity extraction jobs, staging images and cached results."

    def handle(self, *args, **options):
        # Run the sweep logic synchronously (shared task body).
        cleanup_identity_extraction_jobs(job_uuid=None)
        self.stdout.write(self.style.SUCCESS("Identity extraction cleanup complete."))
