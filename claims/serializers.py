from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import serializers

from accounts.serializers import RejectUnknownFieldsMixin
from accounts.services import normalize_email
from claims.models import ClaimIdentityEvidence, PatientAccountClaim
from guardians.models import GuardianRelationship
from identities.models import IdentityDocument
from identities.services import inspect_identity_upload
from patients.models import PatientProfile


class AccountClaimSubmissionSerializer(
    RejectUnknownFieldsMixin, serializers.Serializer
):
    digital_id = serializers.RegexField(r"^\d{17}$")
    email = serializers.EmailField()
    phone = serializers.RegexField(r"^\+?[1-9]\d{7,14}$")
    full_name = serializers.CharField(max_length=255)
    date_of_birth = serializers.DateField()
    identity_document_type = serializers.CharField(max_length=32)
    identity_document_number = serializers.CharField(max_length=128)
    front_image = serializers.ImageField(write_only=True)
    back_image = serializers.ImageField(write_only=True)
    passport_number = serializers.CharField(max_length=128, required=False)
    passport_issuing_country = serializers.RegexField(r"^[A-Z]{2}$", required=False)
    passport_issue_date = serializers.DateField(required=False)
    passport_expiry_date = serializers.DateField(required=False)
    passport_front_image = serializers.ImageField(write_only=True, required=False)
    passport_back_image = serializers.ImageField(write_only=True, required=False)

    def validate_email(self, value):
        return normalize_email(value)

    def validate_identity_document_type(self, value):
        if value != ClaimIdentityEvidence.DocumentType.UNIFIED_NATIONAL_CARD:
            raise serializers.ValidationError("Unified National Card is required.")
        return value

    def _inspect(self, attrs, field):
        if upload := attrs.get(field):
            try:
                inspect_identity_upload(upload)
            except DjangoValidationError as exc:
                raise serializers.ValidationError({field: exc.messages}) from exc

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if attrs["date_of_birth"] > timezone.localdate():
            raise serializers.ValidationError(
                {"date_of_birth": ["Date of birth cannot be in the future."]}
            )
        self._inspect(attrs, "front_image")
        self._inspect(attrs, "back_image")
        passport_fields = {
            "passport_number",
            "passport_issuing_country",
            "passport_issue_date",
            "passport_expiry_date",
            "passport_front_image",
        }
        supplied = passport_fields.intersection(attrs)
        if supplied and supplied != passport_fields:
            missing = passport_fields - supplied
            raise serializers.ValidationError(
                {
                    field: ["This field is required with passport evidence."]
                    for field in missing
                }
            )
        if supplied:
            self._inspect(attrs, "passport_front_image")
            self._inspect(attrs, "passport_back_image")
            if attrs["passport_expiry_date"] <= attrs["passport_issue_date"]:
                raise serializers.ValidationError(
                    {"passport_expiry_date": ["Expiry must be after issue date."]}
                )
        return attrs


class ClaimReceiptSerializer(serializers.Serializer):
    claim_id = serializers.UUIDField(read_only=True)
    status = serializers.CharField(read_only=True)


class ClaimEvidenceSerializer(serializers.ModelSerializer):
    available_images = serializers.SerializerMethodField()

    class Meta:
        model = ClaimIdentityEvidence
        fields = (
            "uuid",
            "document_type",
            "document_number",
            "issuing_country",
            "issue_date",
            "expiry_date",
            "available_images",
        )

    def get_available_images(self, obj) -> list[str]:
        result = ["front"]
        if obj.back_image_id:
            result.append("back")
        return result


class ExistingPatientIdentitySerializer(serializers.ModelSerializer):
    class Meta:
        model = PatientProfile
        fields = ("digital_id", "full_name", "date_of_birth", "identity_status")
        read_only_fields = fields


class ExistingIdentityDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = IdentityDocument
        fields = (
            "uuid",
            "document_type",
            "document_number",
            "verification_status",
            "status",
            "created_at",
        )
        read_only_fields = fields


class GuardianHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = GuardianRelationship
        fields = (
            "uuid",
            "relationship",
            "verification_status",
            "active",
            "started_at",
            "ended_at",
            "ended_reason",
        )
        read_only_fields = fields


class AccountClaimReviewSerializer(serializers.ModelSerializer):
    digital_id = serializers.CharField(source="patient.digital_id", read_only=True)
    identity_evidence = ClaimEvidenceSerializer(many=True, read_only=True)
    existing_identity = ExistingPatientIdentitySerializer(
        source="patient", read_only=True
    )
    identity_history = ExistingIdentityDocumentSerializer(
        source="patient.identity_documents", many=True, read_only=True
    )
    guardian_history = GuardianHistorySerializer(
        source="patient.guardian_relationships", many=True, read_only=True
    )

    class Meta:
        model = PatientAccountClaim
        fields = (
            "uuid",
            "digital_id",
            "requested_email",
            "requested_phone",
            "submitted_name",
            "submitted_date_of_birth",
            "status",
            "name_comparison",
            "date_of_birth_comparison",
            "document_number_comparison",
            "existing_identity",
            "identity_history",
            "guardian_history",
            "rejection_reason",
            "identity_evidence",
            "created_at",
            "updated_at",
        )


class ClaimStatusFilterSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=PatientAccountClaim.Status, required=False)


class ClaimDecisionSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    reason = serializers.CharField(max_length=2000)


class ClaimApprovalSerializer(serializers.Serializer):
    claim_id = serializers.UUIDField(read_only=True)
    user_id = serializers.UUIDField(read_only=True)
    status = serializers.CharField(read_only=True)
    activation_token = serializers.CharField(read_only=True)
    activation_expires_at = serializers.DateTimeField(read_only=True)


class ClaimActivationSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    token = serializers.CharField(min_length=20, max_length=256, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)


class ClaimActivationMessageSerializer(serializers.Serializer):
    message = serializers.CharField(read_only=True)
