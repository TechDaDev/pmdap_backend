from django.contrib import admin

from common.admin import ReadOnlyAdminMixin
from processing.models import DateCandidate, DocumentText, DocumentTextPage


@admin.register(DocumentText)
class DocumentTextAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Metadata-only view of extracted medical text.

    The `text` field (and any OCR snippet) is never rendered — list pages show
    metadata only and the detail form excludes the sensitive body.
    """

    list_display = (
        "document",
        "page_count",
        "character_count",
        "usable",
        "extraction_method",
        "pipeline_version",
    )
    list_filter = ("usable", "extraction_method", "extractor_name")
    fields = (
        "uuid",
        "document",
        "page_count",
        "character_count",
        "meaningful_character_count",
        "usable",
        "usability_reason",
        "has_pages_requiring_ocr",
        "extraction_method",
        "extractor_name",
        "extractor_version",
        "pipeline_version",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields


@admin.register(DocumentTextPage)
class DocumentTextPageAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Metadata-only view of a text page.

    `text`, `native_text` and `ocr_text` are excluded from the form entirely.
    """

    list_display = (
        "document_text",
        "page_number",
        "requires_ocr",
        "ocr_completed",
        "effective_source",
        "ocr_mean_confidence",
    )
    list_filter = ("requires_ocr", "ocr_completed", "effective_source")
    fields = (
        "uuid",
        "document_text",
        "page_number",
        "meaningful_character_count",
        "requires_ocr",
        "ocr_completed",
        "effective_source",
        "ocr_engine_name",
        "ocr_engine_version",
        "ocr_mean_confidence",
        "ocr_minimum_confidence",
        "ocr_duration_ms",
        "preprocessing_version",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields


@admin.register(DateCandidate)
class DateCandidateAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Metadata-only view of detected date candidates.

    `raw_value`, `normalized_value` and `context` (OCR snippets) are excluded
    from the form; list pages never render them.
    """

    list_display = (
        "document",
        "detected_date",
        "candidate_type",
        "score",
        "page_number",
        "source",
        "is_suggested",
        "is_current",
    )
    list_filter = ("candidate_type", "source", "is_suggested", "is_current")
    fields = (
        "uuid",
        "document",
        "detected_date",
        "alternative_date",
        "candidate_type",
        "score",
        "page_number",
        "source",
        "occurrence_index",
        "ambiguous",
        "parsing_rule",
        "pipeline_version",
        "is_suggested",
        "candidate_set_uuid",
        "is_current",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields
