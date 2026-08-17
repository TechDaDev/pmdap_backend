from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import serializers

from documents.models import MedicalDocument, StoredFile
from documents.validation import inspect_medical_upload
from facilities.serializers import HealthcareFacilitySerializer
from processing.models import DateCandidate


class RejectUnknownFieldsMixin:
    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {field: ["This field is not allowed."] for field in sorted(unknown)}
            )
        return super().to_internal_value(data)


class MedicalDocumentMetadataSerializer(
    RejectUnknownFieldsMixin,
    serializers.Serializer,
):
    document_type = serializers.ChoiceField(
        choices=MedicalDocument.DocumentType.choices,
        required=False,
    )
    title = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )
    description = serializers.CharField(
        max_length=5000,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )
    healthcare_facility_id = serializers.UUIDField(required=False, allow_null=True)
    facility_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True, trim_whitespace=True
    )
    location_text = serializers.CharField(
        max_length=255, required=False, allow_blank=True, trim_whitespace=True
    )
    department = serializers.CharField(
        max_length=255, required=False, allow_blank=True, trim_whitespace=True
    )
    physician_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True, trim_whitespace=True
    )


class MedicalDocumentUploadSerializer(MedicalDocumentMetadataSerializer):
    file = serializers.FileField()
    document_date = serializers.DateField(required=False, allow_null=True)
    document_type = serializers.ChoiceField(
        choices=MedicalDocument.DocumentType.choices
    )

    def validate_document_date(self, value):
        if value is not None and value > timezone.localdate():
            raise serializers.ValidationError("Document date cannot be in the future.")
        return value

    def validate_file(self, value):
        try:
            inspect_medical_upload(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return value


class StoredFilePublicSerializer(serializers.ModelSerializer):
    class Meta:
        model = StoredFile
        fields = (
            "original_filename",
            "mime_type",
            "size_bytes",
            "page_count",
            "integrity_status",
            "malware_scan_status",
        )
        read_only_fields = fields


class MedicalDocumentSerializer(serializers.ModelSerializer):
    file = StoredFilePublicSerializer(source="stored_file", read_only=True)
    healthcare_facility = HealthcareFacilitySerializer(read_only=True)

    class Meta:
        model = MedicalDocument
        fields = (
            "uuid",
            "document_type",
            "classification_source",
            "title",
            "description",
            "document_date",
            "date_source",
            "date_verified",
            "date_verified_at",
            "facility_name",
            "healthcare_facility",
            "location_text",
            "department",
            "physician_name",
            "processing_status",
            "archive_status",
            "file",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class MedicalDocumentDetailSerializer(MedicalDocumentSerializer):
    text_available = serializers.SerializerMethodField()

    class Meta(MedicalDocumentSerializer.Meta):
        fields = (*MedicalDocumentSerializer.Meta.fields, "text_available")

    def get_text_available(self, document) -> bool:
        return hasattr(document, "document_text")


class PendingDateConfirmationCandidateSerializer(serializers.ModelSerializer):
    """Safe subset of a date candidate for the confirm queue (no OCR context)."""

    date = serializers.DateField(source="detected_date", read_only=True)
    confidence = serializers.FloatField(source="score", read_only=True)
    type = serializers.CharField(source="candidate_type", read_only=True)

    class Meta:
        model = DateCandidate
        fields = ("uuid", "date", "confidence", "type", "ambiguous", "is_suggested")
        read_only_fields = fields


class PendingDateConfirmationSerializer(serializers.Serializer):
    """Document-centric confirm-dates queue item.

    The document is the unit — it is returned even when OCR found no date
    (`detected_candidates` empty, `requires_manual_date` true).
    """

    document_uuid = serializers.UUIDField()
    document_type = serializers.CharField()
    processing_status = serializers.CharField()
    created_at = serializers.DateTimeField()
    detected_candidates = PendingDateConfirmationCandidateSerializer(many=True)
    requires_manual_date = serializers.BooleanField()


class DateCandidateSerializer(serializers.ModelSerializer):
    date = serializers.DateField(source="detected_date", read_only=True)
    type = serializers.CharField(source="candidate_type", read_only=True)
    score = serializers.FloatField(read_only=True)

    class Meta:
        model = DateCandidate
        fields = (
            "uuid",
            "date",
            "alternative_date",
            "type",
            "score",
            "page_number",
            "context",
            "source",
            "ambiguous",
            "is_suggested",
        )
        read_only_fields = fields


class DocumentDateConfirmationSerializer(
    RejectUnknownFieldsMixin,
    serializers.Serializer,
):
    candidate_id = serializers.UUIDField(required=False)
    date = serializers.DateField(required=False)

    def validate(self, attrs):
        if ("candidate_id" in attrs) == ("date" in attrs):
            from documents.exceptions import InvalidDateConfirmation

            raise InvalidDateConfirmation()
        if "date" in attrs and attrs["date"] > timezone.localdate():
            from documents.exceptions import InvalidDocumentDate

            raise InvalidDocumentDate()
        return attrs


class DocumentDateConfirmationResponseSerializer(serializers.ModelSerializer):
    class Meta:
        model = MedicalDocument
        fields = (
            "uuid",
            "document_date",
            "date_source",
            "date_verified",
            "date_verified_at",
            "processing_status",
        )
        read_only_fields = fields
