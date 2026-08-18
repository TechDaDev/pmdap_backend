from django.contrib import admin

from common.admin import ReadOnlyAdminMixin
from labs.models import LabReportExtraction, LabResult


@admin.register(LabReportExtraction)
class LabReportExtractionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Metadata-only view of a structured lab extraction.

    No lab values are rendered on list or detail pages.
    """

    list_display = (
        "document",
        "status",
        "pipeline_version",
        "result_count",
        "extraction_confidence",
        "created_at",
    )
    list_filter = ("status", "pipeline_version")
    fields = (
        "uuid",
        "document",
        "pipeline_version",
        "status",
        "error_code",
        "result_count",
        "extraction_confidence",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields


@admin.register(LabResult)
class LabResultAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Read-only inspection of a structured lab row.

    ``list_display`` shows metadata only (never result/reference values).
    Detail form exposes values read-only for superuser/internal inspection.
    """

    list_display = (
        "extraction",
        "page_number",
        "row_index",
        "test_name_normalized",
        "unit_normalized",
        "flag_raw",
        "extraction_confidence",
    )
    list_filter = ("extraction__pipeline_version",)
    fields = (
        "uuid",
        "extraction",
        "page_number",
        "row_index",
        "test_name_raw",
        "test_name_normalized",
        "result_raw",
        "result_numeric",
        "result_text",
        "unit_raw",
        "unit_normalized",
        "reference_range_raw",
        "reference_low",
        "reference_high",
        "flag_raw",
        "extraction_confidence",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields
