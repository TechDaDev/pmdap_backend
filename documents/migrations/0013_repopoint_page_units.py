"""Re-point existing DateCandidate / LabReportExtraction rows at their page
unit now that the page_unit FKs exist (processing.0007 / labs.0002).

Runs AFTER documents.0012 created page units, so unambiguous single-page and
page-number matches can be linked without touching the field that the 0012
backfill could not see (FieldDoesNotExist on prod with real rows).
"""
from django.db import migrations


def repoint_page_units(apps, schema_editor):
    MedicalDocumentPage = apps.get_model("documents", "MedicalDocumentPage")
    DateCandidate = apps.get_model("processing", "DateCandidate")
    LabReportExtraction = apps.get_model("labs", "LabReportExtraction")

    for candidate in DateCandidate.objects.all().iterator():
        unit = MedicalDocumentPage.objects.filter(
            document_id=candidate.document_id,
            page_number=candidate.page_number,
        ).first()
        if unit is not None:
            DateCandidate.objects.filter(pk=candidate.pk).update(page_unit=unit)

    for extraction in LabReportExtraction.objects.all().iterator():
        page_count = MedicalDocumentPage.objects.filter(
            document_id=extraction.document_id
        ).count()
        if page_count == 1:
            unit = MedicalDocumentPage.objects.filter(
                document_id=extraction.document_id
            ).first()
            if unit is not None:
                LabReportExtraction.objects.filter(pk=extraction.pk).update(
                    page_unit=unit
                )


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0012_alter_medicaldocument_processing_status_and_more"),
        ("processing", "0007_datecandidate_page_unit"),
        ("labs", "0003_remove_labreportextraction_labs_unique_document_pipeline_version_and_more"),
    ]

    operations = [
        migrations.RunPython(repoint_page_units, migrations.RunPython.noop),
    ]
