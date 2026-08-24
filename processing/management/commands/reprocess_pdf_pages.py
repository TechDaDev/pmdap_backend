"""Reprocess page report units for an existing document.

M25: scanned PDFs ingested via the native-text path had no OCR spans, so page
lab extraction produced zero rows and the wrong subtype. This re-runs date
detection + structured lab extraction (with lazy per-page OCR when spans are
missing) for every page unit of the given document. Idempotent: re-running
uses the current LAB_PIPELINE_VERSION and replaces only that version's rows.

Usage: manage.py reprocess_pdf_pages <document_uuid>
"""
from django.core.management.base import BaseCommand, CommandError

from documents.models import MedicalDocument
from labs.services import process_lab_extraction_for_page
from processing.date_services import process_page_date_candidates


class Command(BaseCommand):
    help = "Reprocess page report units for an existing document (M25)."

    def add_arguments(self, parser):
        parser.add_argument("document_uuid", type=str)

    def handle(self, *args, **options):
        try:
            document = MedicalDocument.objects.get(uuid=options["document_uuid"])
        except MedicalDocument.DoesNotExist as exc:
            raise CommandError("Document not found") from exc
        report = []
        for page in document.pages.order_by("page_number"):
            if not page.date_verified:
                process_page_date_candidates(str(page.uuid))
            process_lab_extraction_for_page(str(page.uuid))
            page.refresh_from_db()
            from labs.models import LabReportExtraction

            ext = LabReportExtraction.objects.filter(page_unit=page).first()
            report.append(
                f"page {page.page_number}: status={page.processing_status} "
                f"subtype={page.report_subtype} "
                f"extraction={ext.status if ext else 'NONE'} "
                f"rows={ext.result_count if ext else 0}"
            )
        from documents.page_services import recalculate_document_processing_state

        document.refresh_from_db()
        parent = recalculate_document_processing_state(document)
        self.stdout.write("\n".join(report))
        self.stdout.write(f"parent={parent}")
