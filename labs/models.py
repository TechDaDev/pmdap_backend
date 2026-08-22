from django.db import models

from common.models import UUIDModel


class LabReportExtraction(UUIDModel):
    """Structured lab extraction for one MedicalDocument.

    The archive-first rule stands: a failed or missing lab extraction never
    invalidates the archived document or its OCR body. This model records the
    outcome (including failure) so the pipeline is traceable.
    """

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        COMPLETED = "COMPLETED", "Completed"
        NOT_APPLICABLE = "NOT_APPLICABLE", "Not applicable"
        FAILED = "FAILED", "Failed"

    document = models.ForeignKey(
        "documents.MedicalDocument",
        on_delete=models.PROTECT,
        related_name="lab_extractions",
    )
    page_unit = models.ForeignKey(
        "documents.MedicalDocumentPage",
        on_delete=models.CASCADE,
        related_name="lab_extractions",
        null=True,
        blank=True,
    )
    pipeline_version = models.CharField(max_length=32)
    status = models.CharField(max_length=16, choices=Status, default=Status.QUEUED)
    error_code = models.CharField(max_length=64, blank=True, default="")
    result_count = models.PositiveIntegerField(default=0)
    extraction_confidence = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("document", "pipeline_version"),
                condition=models.Q(page_unit__isnull=True),
                name="labs_unique_document_pipeline_version",
            ),
            models.UniqueConstraint(
                fields=("page_unit", "pipeline_version"),
                condition=models.Q(page_unit__isnull=False),
                name="labs_unique_page_unit_pipeline_version",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(extraction_confidence__isnull=True)
                    | models.Q(
                        extraction_confidence__gte=0.0,
                        extraction_confidence__lte=1.0,
                    )
                ),
                name="labs_extraction_confidence_range",
            ),
        ]

    def __str__(self):
        return f"{self.document_id}:{self.pipeline_version}:{self.status}"


class LabResult(UUIDModel):
    """One structured lab row with raw + normalized fields and span evidence.

    Raw fields are authoritative; normalized fields are conservative helpers
    only. PMDAP records printed flags but never derives clinical meaning
    (HIGH/LOW/ABNORMAL) itself.
    """

    extraction = models.ForeignKey(
        LabReportExtraction,
        on_delete=models.CASCADE,
        related_name="results",
    )
    page_number = models.PositiveIntegerField()
    row_index = models.PositiveIntegerField()
    test_name_raw = models.TextField(blank=True)
    test_name_normalized = models.CharField(max_length=128, blank=True, default="")
    result_raw = models.TextField(blank=True, default="")
    result_numeric = models.DecimalField(
        max_digits=20, decimal_places=6, null=True, blank=True
    )
    result_text = models.TextField(blank=True, default="")
    unit_raw = models.CharField(max_length=64, blank=True, default="")
    unit_normalized = models.CharField(max_length=64, blank=True, default="")
    reference_range_raw = models.TextField(blank=True, default="")
    reference_low = models.DecimalField(
        max_digits=20, decimal_places=6, null=True, blank=True
    )
    reference_high = models.DecimalField(
        max_digits=20, decimal_places=6, null=True, blank=True
    )
    flag_raw = models.CharField(max_length=16, blank=True, default="")
    extraction_confidence = models.FloatField()
    source_spans = models.ManyToManyField(
        "processing.DocumentTextSpan",
        blank=True,
        related_name="lab_results",
    )

    class Meta:
        ordering = ("page_number", "row_index")
        indexes = [
            models.Index(
                fields=("extraction", "page_number", "row_index"),
                name="labs_result_position_idx",
            ),
            models.Index(
                fields=("test_name_normalized",),
                name="labs_result_name_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(page_number__gte=1),
                name="labs_result_page_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(row_index__gte=0),
                name="labs_result_row_nonnegative",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(extraction_confidence__gte=0.0)
                    & models.Q(extraction_confidence__lte=1.0)
                ),
                name="labs_result_confidence_range",
            ),
        ]

    def __str__(self):
        return f"{self.extraction_id}:{self.page_number}:{self.row_index}"
