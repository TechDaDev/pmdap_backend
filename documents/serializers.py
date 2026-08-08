from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import serializers

from documents.models import MedicalDocument, StoredFile
from documents.validation import inspect_medical_upload


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
    document_date = serializers.DateField(required=False, allow_null=True)
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

    def validate_document_date(self, value):
        if value is not None and value > timezone.localdate():
            raise serializers.ValidationError("Document date cannot be in the future.")
        return value


class MedicalDocumentUploadSerializer(MedicalDocumentMetadataSerializer):
    file = serializers.FileField()
    document_type = serializers.ChoiceField(
        choices=MedicalDocument.DocumentType.choices
    )

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

    class Meta:
        model = MedicalDocument
        fields = (
            "uuid",
            "document_type",
            "title",
            "description",
            "document_date",
            "date_source",
            "date_verified",
            "date_verified_at",
            "facility_name",
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
