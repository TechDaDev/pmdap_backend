from django.db import models

from common.models import UUIDModel


class DocumentText(UUIDModel):
    class ExtractionMethod(models.TextChoices):
        PDF_TEXT = "PDF_TEXT", "PDF text"
        OCR = "OCR", "OCR"
        HYBRID = "HYBRID", "Hybrid"

    document = models.OneToOneField(
        "documents.MedicalDocument",
        on_delete=models.PROTECT,
        related_name="document_text",
    )
    text = models.TextField(blank=True)
    page_count = models.PositiveIntegerField()
    character_count = models.PositiveIntegerField()
    meaningful_character_count = models.PositiveIntegerField()
    usable = models.BooleanField()
    usability_reason = models.CharField(max_length=64)
    has_pages_requiring_ocr = models.BooleanField(default=False)
    extraction_method = models.CharField(max_length=16, choices=ExtractionMethod)
    extractor_name = models.CharField(max_length=64)
    extractor_version = models.CharField(max_length=32)
    pipeline_version = models.CharField(max_length=32)
    ocr_engine_name = models.CharField(max_length=64, blank=True, default="")
    ocr_engine_version = models.CharField(max_length=32, blank=True, default="")
    ocr_pipeline_version = models.CharField(max_length=32, blank=True, default="")

    def __str__(self):
        return str(self.uuid)


class DocumentTextPage(UUIDModel):
    class EffectiveSource(models.TextChoices):
        PDF_TEXT = "PDF_TEXT", "PDF text"
        OCR = "OCR", "OCR"

    document_text = models.ForeignKey(
        DocumentText,
        on_delete=models.CASCADE,
        related_name="pages",
    )
    page_number = models.PositiveIntegerField()
    text = models.TextField(blank=True)
    native_text = models.TextField(blank=True, default="")
    ocr_text = models.TextField(blank=True, default="")
    meaningful_character_count = models.PositiveIntegerField()
    requires_ocr = models.BooleanField(default=False)
    ocr_completed = models.BooleanField(default=False)
    effective_source = models.CharField(
        max_length=16,
        choices=EffectiveSource,
        default=EffectiveSource.PDF_TEXT,
    )
    ocr_engine_name = models.CharField(max_length=64, blank=True, default="")
    ocr_engine_version = models.CharField(max_length=32, blank=True, default="")
    ocr_mean_confidence = models.FloatField(null=True, blank=True)
    ocr_minimum_confidence = models.FloatField(null=True, blank=True)
    ocr_duration_ms = models.PositiveIntegerField(null=True, blank=True)
    preprocessing_version = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        ordering = ("page_number",)
        constraints = [
            models.UniqueConstraint(
                fields=("document_text", "page_number"),
                name="processing_unique_document_text_page",
            ),
            models.CheckConstraint(
                condition=models.Q(page_number__gte=1),
                name="processing_page_number_positive",
            ),
        ]

    def __str__(self):
        return f"{self.document_text_id}:{self.page_number}"
