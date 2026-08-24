from rest_framework import serializers

from documents.models import MedicalDocument
from documents.serializers import RejectUnknownFieldsMixin, StoredFilePublicSerializer
from facilities.models import HealthcareFacility


class ArchiveFacilitySummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = HealthcareFacility
        fields = ("uuid", "name")
        read_only_fields = fields


class ArchiveFilterSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    date_status = serializers.ChoiceField(
        choices=("VERIFIED", "UNCONFIRMED"),
        required=False,
    )
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
    page = serializers.IntegerField(min_value=1, required=False)

    def validate(self, attrs):
        month = attrs.get("month")
        year = attrs.get("year")
        if month is not None and year is None:
            raise serializers.ValidationError(
                {"month": "Month filter requires a year."}
            )
        if attrs.get("date_status") == "UNCONFIRMED" and (
            year is not None or month is not None
        ):
            raise serializers.ValidationError(
                {
                    "date_status": (
                        "UNCONFIRMED date status cannot be combined with "
                        "year or month filters."
                    )
                }
            )
        return attrs


class ArchiveDocumentSerializer(serializers.ModelSerializer):
    healthcare_facility = ArchiveFacilitySummarySerializer(read_only=True)
    file = StoredFilePublicSerializer(source="stored_file", read_only=True)

    class Meta:
        model = MedicalDocument
        fields = (
            "uuid",
            "title",
            "document_type",
            "document_date",
            "date_verified",
            "date_source",
            "healthcare_facility",
            "facility_name",
            "location_text",
            "department",
            "physician_name",
            "processing_status",
            "created_at",
            "file",
        )
        read_only_fields = fields


class ArchiveSummaryMonthSerializer(serializers.Serializer):
    month = serializers.IntegerField()
    count = serializers.IntegerField()


class ArchiveSummaryYearSerializer(serializers.Serializer):
    year = serializers.IntegerField()
    count = serializers.IntegerField()
    months = ArchiveSummaryMonthSerializer(many=True)


class ArchiveSummaryDocumentTypeSerializer(serializers.Serializer):
    document_type = serializers.ChoiceField(
        choices=MedicalDocument.DocumentType.choices
    )
    count = serializers.IntegerField()


class ArchiveSummaryFacilitySerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    name = serializers.CharField()
    count = serializers.IntegerField()


class ArchiveSummarySerializer(serializers.Serializer):
    years = ArchiveSummaryYearSerializer(many=True)
    document_types = ArchiveSummaryDocumentTypeSerializer(many=True)
    facilities = ArchiveSummaryFacilitySerializer(many=True)
    unconfirmed_date_count = serializers.IntegerField()
