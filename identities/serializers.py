from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import serializers

from identities.models import IdentityDocument
from identities.services import inspect_identity_upload


class RejectUnknownFieldsMixin:
    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {field: ["This field is not allowed."] for field in sorted(unknown)}
            )
        return super().to_internal_value(data)


class IdentityExtractionRequestSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    """Advisory extraction request. No IdentityDocument is created."""

    EXTRACTABLE_TYPES = (
        IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
        IdentityDocument.DocumentType.PASSPORT,
    )

    document_type = serializers.ChoiceField(choices=EXTRACTABLE_TYPES)
    front_image = serializers.ImageField()
    back_image = serializers.ImageField(required=False, allow_null=True)

    def validate_front_image(self, value):
        try:
            inspect_identity_upload(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return value

    def validate_back_image(self, value):
        return self.validate_front_image(value)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if (
            attrs["document_type"] == IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD
            and not attrs.get("back_image")
        ):
            raise serializers.ValidationError(
                {"back_image": ["A back image is required for a National Card."]}
            )
        return attrs


class ExtractedIdentityFieldSerializer(serializers.Serializer):
    value = serializers.CharField(allow_null=True, required=False)
    confidence = serializers.FloatField(min_value=0.0, max_value=1.0)
    source = serializers.ChoiceField(
        choices=("MRZ", "OCR", "DOCUMENT_TYPE", "DERIVED")
    )


class MrzValidationSerializer(serializers.Serializer):
    detected = serializers.BooleanField()
    valid = serializers.BooleanField()
    checks_passed = serializers.BooleanField()


class IdentityExtractionResponseSerializer(serializers.Serializer):
    document_type = serializers.CharField()
    extractor_version = serializers.CharField()
    fields = serializers.DictField(child=ExtractedIdentityFieldSerializer())
    warnings = serializers.ListField(child=serializers.CharField())
    mrz = MrzValidationSerializer()


class IdentityDocumentInputSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    document_type = serializers.ChoiceField(
        choices=IdentityDocument.DocumentType.choices
    )
    document_number = serializers.CharField(max_length=128)
    national_number = serializers.CharField(
        max_length=128, required=False, allow_blank=True, default=""
    )
    family_number = serializers.CharField(
        max_length=128, required=False, allow_blank=True, default=""
    )
    issuing_country = serializers.RegexField(r"^[A-Za-z]{2}$", required=False)
    issue_date = serializers.DateField(required=False, allow_null=True)
    expiry_date = serializers.DateField(required=False, allow_null=True)
    front_image = serializers.FileField(required=False, allow_null=True)
    back_image = serializers.FileField(required=False, allow_null=True)
    extraction_job_id = serializers.UUIDField(required=False, allow_null=True)

    def validate_issuing_country(self, value):
        return value.upper()

    def _validate_image(self, value):
        try:
            inspect_identity_upload(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return value

    def validate_front_image(self, value):
        return self._validate_image(value)

    def validate_back_image(self, value):
        return self._validate_image(value)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        errors = {}
        document_type = attrs["document_type"]
        job_id = attrs.get("extraction_job_id")
        has_job = bool(job_id)
        has_images = attrs.get("front_image") is not None or (
            attrs.get("back_image") is not None
        )

        if has_job and has_images:
            errors["extraction_job_id"] = [
                "Use either extraction_job_id or image files, not both."
            ]
            raise serializers.ValidationError(errors)
        if not has_job and attrs.get("front_image") is None:
            errors["front_image"] = [
                "This field is required when not using an extraction job."
            ]

        if document_type == IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD:
            if "issuing_country" not in attrs:
                attrs["issuing_country"] = "IQ"
            required_fields = ["national_number"]
            if not self.context.get("minor_creation"):
                required_fields.append("family_number")
            for field in required_fields:
                if not attrs.get(field):
                    errors[field] = ["This field is required for a National Card."]
            if not has_job and not attrs.get("back_image"):
                errors["back_image"] = ["This field is required for a National Card."]
            if attrs["issuing_country"] != "IQ":
                errors["issuing_country"] = [
                    "Unified National Card must be issued by Iraq."
                ]
        elif "issuing_country" not in attrs:
            errors["issuing_country"] = ["This field is required."]
        if document_type == IdentityDocument.DocumentType.PASSPORT:
            for field in ("issue_date", "expiry_date"):
                if not attrs.get(field):
                    errors[field] = ["This field is required for a passport."]
        issue_date = attrs.get("issue_date")
        expiry_date = attrs.get("expiry_date")
        if issue_date and issue_date > timezone.localdate():
            errors["issue_date"] = ["Issue date cannot be in the future."]
        if issue_date and expiry_date and expiry_date <= issue_date:
            errors["expiry_date"] = ["Expiry date must be after issue date."]
        if expiry_date and expiry_date < timezone.localdate():
            errors["expiry_date"] = ["Expired identity documents cannot be submitted."]
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class IdentityDocumentSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = IdentityDocument
        fields = (
            "uuid",
            "document_type",
            "issuing_country",
            "issue_date",
            "expiry_date",
            "verification_status",
            "status",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class IdentityDocumentDetailSerializer(IdentityDocumentSummarySerializer):
    available_images = serializers.SerializerMethodField()
    replaces = serializers.UUIDField(source="replaces_id", read_only=True)

    class Meta(IdentityDocumentSummarySerializer.Meta):
        fields = IdentityDocumentSummarySerializer.Meta.fields + (
            "document_number",
            "national_number",
            "family_number",
            "verified_at",
            "rejection_reason",
            "available_images",
            "replaces",
        )

    def get_available_images(self, obj) -> list[str]:
        sides = ["front"]
        if obj.back_image_id:
            sides.append("back")
        return sides


class VerificationPatientSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(read_only=True)
    digital_id = serializers.CharField(read_only=True)
    full_name = serializers.CharField(read_only=True)
    date_of_birth = serializers.DateField(read_only=True)
    sex = serializers.CharField(read_only=True)
    nationality = serializers.CharField(read_only=True)
    identity_status = serializers.CharField(read_only=True)


class VerificationQueueSerializer(IdentityDocumentSummarySerializer):
    patient = VerificationPatientSerializer(read_only=True)

    class Meta(IdentityDocumentSummarySerializer.Meta):
        fields = IdentityDocumentSummarySerializer.Meta.fields + ("patient",)


class VerificationDetailSerializer(IdentityDocumentDetailSerializer):
    patient = VerificationPatientSerializer(read_only=True)

    class Meta(IdentityDocumentDetailSerializer.Meta):
        fields = IdentityDocumentDetailSerializer.Meta.fields + ("patient",)


class VerificationQueueFilterSerializer(
    RejectUnknownFieldsMixin, serializers.Serializer
):
    status = serializers.ChoiceField(
        choices=IdentityDocument.VerificationStatus.choices, required=False
    )


class EmptySerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    pass


class RejectionSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    rejection_reason = serializers.CharField(
        min_length=1, max_length=1000, trim_whitespace=True
    )


class IdentityExtractionJobSerializer(serializers.Serializer):
    """POST /extract/ 202 response: async job created."""

    job_id = serializers.UUIDField()
    status = serializers.CharField()


class IdentityExtractionStatusSerializer(serializers.Serializer):
    """GET /extract/<job_id>/ poll response."""

    job_id = serializers.UUIDField()
    status = serializers.CharField()
    error_code = serializers.CharField(required=False, default="")
    document_type = serializers.CharField(required=False, default="")
    extractor_version = serializers.CharField(required=False, default="")
    fields = serializers.DictField(
        child=ExtractedIdentityFieldSerializer(),
        required=False,
        default=dict,
    )
    warnings = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    mrz = MrzValidationSerializer(required=False)
