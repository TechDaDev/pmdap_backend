from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from identities.models import IdentityDocument, IdentityFieldCorrection
from identities.services import inspect_identity_upload


class RejectUnknownFieldsMixin:
    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {field: ["This field is not allowed."] for field in sorted(unknown)}
            )
        return super().to_internal_value(data)


class IdentityExtractionRequestSerializer(
    RejectUnknownFieldsMixin, serializers.Serializer
):
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
        is_national_card = (
            attrs["document_type"]
            == IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD
        )
        if is_national_card and not attrs.get("back_image"):
            raise serializers.ValidationError(
                {"back_image": ["A back image is required for a National Card."]}
            )
        return attrs


class ExtractedIdentityFieldSerializer(serializers.Serializer):
    value = serializers.CharField(allow_null=True, required=False)
    confidence = serializers.FloatField(min_value=0.0, max_value=1.0)
    source = serializers.ChoiceField(
        choices=(
            "MRZ",
            "OCR",
            "DOCUMENT_TYPE",
            "DERIVED",
            "FRONT_PRINTED",
            "BACK_PRINTED",
            "ROI",
        )
    )
    cross_check = serializers.ChoiceField(
        choices=("MRZ_AGREE", "MRZ_MISMATCH"),
        required=False,
        allow_null=True,
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
    unique_card_body_number = serializers.CharField(
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
            # Never alias the national number into generic document_number.
            # Existing manual clients may still provide the card-body value in
            # document_number; preserve it in the explicit field.
            body_number = attrs.get("unique_card_body_number", "").strip()
            supplied_document_number = attrs.get("document_number", "").strip()
            national_number = attrs.get("national_number", "").strip()
            if not body_number and supplied_document_number != national_number:
                body_number = supplied_document_number
                attrs["unique_card_body_number"] = body_number
            attrs["document_number"] = body_number
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
    card_body_number = serializers.CharField(
        source="unique_card_body_number", read_only=True
    )

    class Meta(IdentityDocumentSummarySerializer.Meta):
        fields = IdentityDocumentSummarySerializer.Meta.fields + (
            "document_number",
            "national_number",
            "family_number",
            "unique_card_body_number",
            "card_body_number",
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


class IdentityReviewFieldSerializer(serializers.Serializer):
    """Per-field review state: original authoritative value, current reviewed
    value (staged or original), and whether the reviewer corrected it."""

    original = serializers.CharField(read_only=True)
    reviewed = serializers.CharField(read_only=True)
    corrected = serializers.BooleanField(read_only=True)


class IdentityCorrectionProvenanceSerializer(serializers.Serializer):
    """Safe correction provenance. Never carries field values."""

    field = serializers.CharField(read_only=True)
    source = serializers.CharField(read_only=True)
    review_version = serializers.IntegerField(read_only=True)
    corrected_by = serializers.UUIDField(read_only=True, allow_null=True)
    corrected_at = serializers.DateTimeField(read_only=True)
    reason_category = serializers.CharField(read_only=True)


class VerificationQueueSerializer(IdentityDocumentSummarySerializer):
    patient = VerificationPatientSerializer(read_only=True)
    has_corrections = serializers.BooleanField(read_only=True)

    class Meta(IdentityDocumentSummarySerializer.Meta):
        fields = IdentityDocumentSummarySerializer.Meta.fields + (
            "patient",
            "has_corrections",
        )

    def get_has_corrections(self, obj):
        return obj.field_corrections.exists()


class VerificationDetailSerializer(IdentityDocumentDetailSerializer):
    patient = VerificationPatientSerializer(read_only=True)
    review_fields = serializers.SerializerMethodField()
    review_version = serializers.IntegerField(read_only=True)
    has_corrections = serializers.SerializerMethodField()
    corrections = serializers.SerializerMethodField()
    available_actions = serializers.SerializerMethodField()

    class Meta(IdentityDocumentDetailSerializer.Meta):
        fields = IdentityDocumentDetailSerializer.Meta.fields + (
            "patient",
            "review_fields",
            "review_version",
            "has_corrections",
            "corrections",
            "available_actions",
        )

    @extend_schema_field(serializers.BooleanField)
    def get_has_corrections(self, obj):
        return obj.field_corrections.exists()

    @extend_schema_field(IdentityCorrectionProvenanceSerializer(many=True))
    def get_corrections(self, obj):
        return [
            {
                "field": c.field,
                "source": c.source,
                "review_version": c.review_version,
                "corrected_by": str(c.corrected_by_id) if c.corrected_by_id else None,
                "corrected_at": c.corrected_at,
                "reason_category": c.reason_category,
            }
            for c in obj.field_corrections.all()
        ]

    @extend_schema_field(
        serializers.DictField(child=IdentityReviewFieldSerializer(read_only=True))
    )
    def get_review_fields(self, obj):
        from identities.corrections import (
            PROFILE_FIELDS,
            REVIEWABLE_FIELDS,
            _as_text,
        )

        profile = obj.patient
        out = {}
        for field in sorted(REVIEWABLE_FIELDS):
            source = profile if field in PROFILE_FIELDS else obj
            original = _as_text(getattr(source, field))
            staged = getattr(obj, f"reviewed_{field}", None)
            reviewed = _as_text(staged) if staged else original
            out[field] = {
                "original": original,
                "reviewed": reviewed,
                "corrected": reviewed != original,
            }
        return out

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_available_actions(self, obj):
        from identities.models import IdentityDocument

        actions = []
        if (
            obj.verification_status == IdentityDocument.VerificationStatus.PENDING
            and obj.status == IdentityDocument.LifecycleStatus.CURRENT
        ):
            actions.extend(["review_fields", "approve", "reject"])
        if (
            obj.verification_status == IdentityDocument.VerificationStatus.VERIFIED
            and obj.status == IdentityDocument.LifecycleStatus.CURRENT
        ):
            actions.append("correct_verified")
        return actions


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


class IdentityReviewFieldsSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    """POST review-fields: reviewer corrections for a PENDING identity.

    Only whitelisted structured identity fields are accepted; the domain
    service re-validates every value and rejects any status/owner/audit field.
    """

    review_version = serializers.IntegerField()
    fields = serializers.DictField()


class VerifiedCorrectionSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    """POST correct-verified: high-risk correction of a VERIFIED identity.

    Requires a non-blank reason category; a note is optional and limited.
    """

    review_version = serializers.IntegerField()
    fields = serializers.DictField()
    reason_category = serializers.ChoiceField(
        choices=IdentityFieldCorrection.ReasonCategory.choices
    )
    note = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=500
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
