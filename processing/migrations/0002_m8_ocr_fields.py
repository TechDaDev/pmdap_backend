from django.db import migrations, models


def preserve_native_text(apps, schema_editor):
    DocumentTextPage = apps.get_model("processing", "DocumentTextPage")
    DocumentTextPage.objects.update(
        native_text=models.F("text"),
        effective_source="PDF_TEXT",
    )


class Migration(migrations.Migration):
    dependencies = [("processing", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="documenttext",
            name="ocr_engine_name",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="documenttext",
            name="ocr_engine_version",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="documenttext",
            name="ocr_pipeline_version",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AlterField(
            model_name="documenttext",
            name="extraction_method",
            field=models.CharField(
                choices=[
                    ("PDF_TEXT", "PDF text"),
                    ("OCR", "OCR"),
                    ("HYBRID", "Hybrid"),
                ],
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="documenttextpage",
            name="effective_source",
            field=models.CharField(
                choices=[("PDF_TEXT", "PDF text"), ("OCR", "OCR")],
                default="PDF_TEXT",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="documenttextpage",
            name="native_text",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="documenttextpage",
            name="ocr_completed",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="documenttextpage",
            name="ocr_duration_ms",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="documenttextpage",
            name="ocr_engine_name",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="documenttextpage",
            name="ocr_engine_version",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="documenttextpage",
            name="ocr_mean_confidence",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="documenttextpage",
            name="ocr_minimum_confidence",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="documenttextpage",
            name="ocr_text",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="documenttextpage",
            name="preprocessing_version",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.RunPython(preserve_native_text, migrations.RunPython.noop),
    ]
