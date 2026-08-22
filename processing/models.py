import uuid

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


class DocumentTextSpan(UUIDModel):
    """One OCR line with normalized geometry.

    Canonical readable text lives in ``DocumentText`` / ``DocumentTextPage``.
    Spans are supplemental spatial evidence (used by structured lab
    extraction). Coordinates are normalized to the page (0.0-1.0) so parsing is
    independent of source resolution; ``page_width`` / ``page_height`` retain
    the source pixels the OCR engine actually saw.
    """

    class Source(models.TextChoices):
        OCR = "OCR", "OCR"
        PDF_TEXT = "PDF_TEXT", "PDF text"

    document_text_page = models.ForeignKey(
        DocumentTextPage,
        on_delete=models.CASCADE,
        related_name="spans",
    )
    sequence = models.PositiveIntegerField()
    text = models.TextField(blank=True)
    confidence = models.FloatField()
    x_min = models.FloatField()
    y_min = models.FloatField()
    x_max = models.FloatField()
    y_max = models.FloatField()
    source = models.CharField(max_length=16, choices=Source, default=Source.OCR)
    page_width = models.PositiveIntegerField()
    page_height = models.PositiveIntegerField()

    class Meta:
        ordering = ("sequence",)
        constraints = [
            models.UniqueConstraint(
                fields=("document_text_page", "sequence"),
                name="processing_unique_document_text_span_sequence",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(x_min__gte=0.0)
                    & models.Q(y_min__gte=0.0)
                    & models.Q(x_max__lte=1.0)
                    & models.Q(y_max__lte=1.0)
                    & models.Q(x_min__lte=models.F("x_max"))
                    & models.Q(y_min__lte=models.F("y_max"))
                ),
                name="processing_span_normalized_bounds",
            ),
            models.CheckConstraint(
                condition=models.Q(sequence__gte=0),
                name="processing_span_sequence_nonnegative",
            ),
        ]

    def __str__(self):
        return f"{self.document_text_page_id}:{self.sequence}"


class DateCandidate(UUIDModel):
    class CandidateType(models.TextChoices):
        REPORT_DATE = "REPORT_DATE", "Report date"
        RESULT_DATE = "RESULT_DATE", "Result date"
        ISSUE_DATE = "ISSUE_DATE", "Issue date"
        COLLECTION_DATE = "COLLECTION_DATE", "Collection date"
        SAMPLE_DATE = "SAMPLE_DATE", "Sample date"
        EXAMINATION_DATE = "EXAMINATION_DATE", "Examination date"
        ADMISSION_DATE = "ADMISSION_DATE", "Admission date"
        DISCHARGE_DATE = "DISCHARGE_DATE", "Discharge date"
        APPLICATION_DATE = "APPLICATION_DATE", "Application date"
        PRINT_DATE = "PRINT_DATE", "Print date"
        DATE_OF_BIRTH = "DATE_OF_BIRTH", "Date of birth"
        UNKNOWN = "UNKNOWN", "Unknown"

    class Source(models.TextChoices):
        PDF_TEXT = "PDF_TEXT", "PDF text"
        OCR = "OCR", "OCR"

    document = models.ForeignKey(
        "documents.MedicalDocument",
        on_delete=models.PROTECT,
        related_name="date_candidates",
    )
    page_unit = models.ForeignKey(
        "documents.MedicalDocumentPage",
        on_delete=models.CASCADE,
        related_name="date_candidates",
        null=True,
        blank=True,
    )
    detected_date = models.DateField()
    alternative_date = models.DateField(null=True, blank=True)
    raw_value = models.CharField(max_length=64)
    normalized_value = models.CharField(max_length=64)
    candidate_type = models.CharField(max_length=24, choices=CandidateType)
    score = models.FloatField()
    page_number = models.PositiveIntegerField()
    context = models.CharField(max_length=256)
    source = models.CharField(max_length=16, choices=Source)
    occurrence_index = models.PositiveIntegerField()
    ambiguous = models.BooleanField(default=False)
    parsing_rule = models.CharField(max_length=32)
    pipeline_version = models.CharField(max_length=32)
    is_suggested = models.BooleanField(default=False)
    candidate_set_uuid = models.UUIDField(default=uuid.uuid4, editable=False)
    is_current = models.BooleanField(default=True)

    class Meta:
        ordering = ("-score", "page_number", "occurrence_index", "uuid")
        indexes = [
            models.Index(
                fields=("document", "is_current", "pipeline_version"),
                name="date_candidate_pipeline_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "document",
                    "candidate_set_uuid",
                    "pipeline_version",
                    "page_number",
                    "occurrence_index",
                    "candidate_type",
                ),
                name="date_candidate_occurrence_unique",
            ),
            models.UniqueConstraint(
                fields=("document",),
                condition=models.Q(is_current=True, is_suggested=True),
                name="date_candidate_one_suggested",
            ),
            models.CheckConstraint(
                condition=models.Q(score__gte=0.0, score__lte=1.0),
                name="date_candidate_score_range",
            ),
            models.CheckConstraint(
                condition=models.Q(page_number__gte=1),
                name="date_candidate_page_positive",
            ),
        ]

    def __str__(self):
        return f"{self.document_id}:{self.detected_date}:{self.candidate_type}"
