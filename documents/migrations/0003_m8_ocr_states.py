from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0002_alter_medicaldocument_processing_status_and_more")
    ]

    operations = [
        migrations.AlterField(
            model_name="medicaldocument",
            name="processing_status",
            field=models.CharField(
                choices=[
                    ("UPLOADED", "Uploaded"),
                    ("QUEUED", "Queued"),
                    ("PROCESSING", "Processing"),
                    ("TEXT_EXTRACTED", "Text extracted"),
                    ("OCR_REQUIRED", "OCR required"),
                    ("OCR_PROCESSING", "OCR processing"),
                    ("DATE_DETECTED", "Date detected"),
                    ("AWAITING_CONFIRMATION", "Awaiting confirmation"),
                    ("INDEXED", "Indexed"),
                    ("FAILED", "Failed"),
                ],
                default="UPLOADED",
                max_length=24,
            ),
        ),
        migrations.AlterField(
            model_name="medicaldocumentevent",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("MEDICAL_DOCUMENT_UPLOADED", "Uploaded"),
                    ("MEDICAL_DOCUMENT_METADATA_UPDATED", "Metadata updated"),
                    ("MEDICAL_DOCUMENT_DELETED", "Deleted"),
                    ("MEDICAL_DOCUMENT_DUPLICATE_REJECTED", "Duplicate rejected"),
                    ("MEDICAL_FILE_INTEGRITY_CHECKED", "File integrity checked"),
                    ("PDF_EXTRACTION_QUEUED", "PDF extraction queued"),
                    ("PDF_EXTRACTION_STARTED", "PDF extraction started"),
                    ("PDF_TEXT_EXTRACTED", "PDF text extracted"),
                    ("PDF_OCR_REQUIRED", "PDF OCR required"),
                    ("PDF_EXTRACTION_FAILED", "PDF extraction failed"),
                    ("OCR_QUEUED", "OCR queued"),
                    ("OCR_STARTED", "OCR started"),
                    ("OCR_PAGE_COMPLETED", "OCR page completed"),
                    ("OCR_COMPLETED", "OCR completed"),
                    ("OCR_FAILED", "OCR failed"),
                ],
                max_length=48,
            ),
        ),
    ]
