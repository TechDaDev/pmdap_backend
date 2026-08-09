from datetime import date

from django.conf import settings
from rest_framework import serializers

from documents.models import MedicalDocument
from documents.serializers import RejectUnknownFieldsMixin

MIN_SUPPORTED_DATE = date(1900, 1, 1)
MAX_SUPPORTED_DATE = date(2100, 12, 31)


class SearchFilterSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    q = serializers.CharField(
        max_length=settings.SEARCH_QUERY_MAX_CHARS,
        required=False,
        allow_blank=False,
        trim_whitespace=True,
    )
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    year = serializers.IntegerField(
        min_value=1900,
        max_value=2100,
        required=False,
    )
    month = serializers.IntegerField(
        min_value=1,
        max_value=12,
        required=False,
    )
    document_type = serializers.ChoiceField(
        choices=MedicalDocument.DocumentType.choices,
        required=False,
    )
    healthcare_facility = serializers.UUIDField(required=False)
    department = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )
    physician_name = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )
    uploaded_from = serializers.DateField(required=False)
    uploaded_to = serializers.DateField(required=False)
    date_status = serializers.ChoiceField(
        choices=("VERIFIED", "UNCONFIRMED"),
        required=False,
    )
    page = serializers.IntegerField(min_value=1, required=False)

    def _validate_supported_date(self, attrs, field):
        value = attrs.get(field)
        if value is not None and not (
            MIN_SUPPORTED_DATE <= value <= MAX_SUPPORTED_DATE
        ):
            raise serializers.ValidationError(
                {
                    field: (
                        "Date must be between "
                        f"{MIN_SUPPORTED_DATE.isoformat()} and "
                        f"{MAX_SUPPORTED_DATE.isoformat()}."
                    )
                }
            )

    def validate(self, attrs):
        month = attrs.get("month")
        year = attrs.get("year")
        if month is not None and year is None:
            raise serializers.ValidationError(
                {"month": "Month filter requires a year."}
            )
        if (
            attrs.get("date_from")
            and attrs.get("date_to")
            and attrs["date_from"] > attrs["date_to"]
        ):
            raise serializers.ValidationError(
                {"date_from": "date_from cannot be after date_to."}
            )
        if (
            attrs.get("uploaded_from")
            and attrs.get("uploaded_to")
            and attrs["uploaded_from"] > attrs["uploaded_to"]
        ):
            raise serializers.ValidationError(
                {"uploaded_from": "uploaded_from cannot be after uploaded_to."}
            )
        for field in ("date_from", "date_to", "uploaded_from", "uploaded_to"):
            self._validate_supported_date(attrs, field)
        if attrs.get("date_status") == "UNCONFIRMED" and any(
            key in attrs for key in ("date_from", "date_to", "year", "month")
        ):
            raise serializers.ValidationError(
                {
                    "date_status": (
                        "UNCONFIRMED date status cannot be combined with "
                        "report-date filters."
                    )
                }
            )
        return attrs
