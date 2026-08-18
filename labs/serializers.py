from decimal import Decimal

from rest_framework import serializers

from labs.models import LabResult


class PlainDecimalField(serializers.DecimalField):
    """Decimal serialized as a clean string (no trailing zeros, no float)."""

    def to_representation(self, value):
        if value is None:
            return None
        normalized = value.normalize()
        if normalized == normalized.to_integral_value():
            normalized = normalized.quantize(Decimal(1))
        return str(normalized)


class LabResultSerializer(serializers.ModelSerializer):
    """Patient-facing structured lab row.

    Raw fields mirror the report wording. Decimal values are serialized as
    clean strings (never binary float). Geometry/OCR evidence is never exposed.
    """

    result_numeric = PlainDecimalField(
        max_digits=20,
        decimal_places=6,
        allow_null=True,
    )
    reference_low = PlainDecimalField(
        max_digits=20,
        decimal_places=6,
        allow_null=True,
    )
    reference_high = PlainDecimalField(
        max_digits=20,
        decimal_places=6,
        allow_null=True,
    )

    class Meta:
        model = LabResult
        fields = (
            "uuid",
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
        )


class LabResultsResponseSerializer(serializers.Serializer):
    document_uuid = serializers.UUIDField()
    document_type = serializers.CharField()
    extraction_status = serializers.CharField()
    pipeline_version = serializers.CharField(allow_null=True)
    result_count = serializers.IntegerField()
    results = LabResultSerializer(many=True)
