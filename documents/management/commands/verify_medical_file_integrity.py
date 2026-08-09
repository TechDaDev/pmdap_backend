from django.core.management.base import BaseCommand

from documents.models import StoredFile
from documents.services import verify_stored_file_integrity


class Command(BaseCommand):
    help = (
        "Verify stored medical file integrity (size + SHA-256) against DB "
        "records. Operates on DB resource IDs only; never reads arbitrary "
        "filesystem paths and never prints file contents."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--document",
            type=str,
            help="Limit verification to one MedicalDocument UUID.",
        )
        parser.add_argument(
            "--patient",
            type=str,
            help="Limit verification to one PatientProfile UUID.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Maximum number of stored files to verify.",
        )

    def handle(self, *args, **options):
        queryset = StoredFile.objects.order_by("created_at", "uuid")
        if document_uuid := options.get("document"):
            queryset = queryset.filter(medical_document__uuid=document_uuid)
        if patient_uuid := options.get("patient"):
            queryset = queryset.filter(medical_document__patient__uuid=patient_uuid)
        if limit := options.get("limit"):
            queryset = queryset[:limit]

        counts = {}
        for stored in queryset.iterator():
            verified = verify_stored_file_integrity(stored)
            status = verified.integrity_status
            counts[status] = counts.get(status, 0) + 1

        self.stdout.write("Medical file integrity verification complete.")
        if not counts:
            self.stdout.write("  no stored files matched.")
        for status in sorted(counts):
            self.stdout.write(f"  {status}: {counts[status]}")
