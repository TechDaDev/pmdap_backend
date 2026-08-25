import re

from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from guardians.exceptions import PatientNotMinor, RelationshipEvidenceRequired
from guardians.models import GuardianEvidence, GuardianRelationship
from identities.models import IdentityDocument
from identities.serializers import (
    IdentityDocumentInputSerializer,
    RejectUnknownFieldsMixin,
)
from patients.models import PatientProfile


class MinorCreateSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    full_name = serializers.CharField(max_length=255)
    date_of_birth = serializers.DateField()
    sex = serializers.ChoiceField(choices=PatientProfile.Sex.choices)
    nationality = serializers.CharField(min_length=2, max_length=2)
    blood_group = serializers.ChoiceField(
        choices=PatientProfile.BloodGroup.choices,
        required=False,
        default=PatientProfile.BloodGroup.UNKNOWN,
    )
    relationship = serializers.ChoiceField(
        choices=GuardianRelationship.Relationship.choices
    )
    document_type = serializers.ChoiceField(
        choices=IdentityDocument.DocumentType.choices
    )
    document_number = serializers.CharField(max_length=128)
    national_number = serializers.CharField(
        max_length=128, required=False, allow_blank=True, default=""
    )
    issuing_country = serializers.RegexField(r"^[A-Za-z]{2}$", required=False)
    issue_date = serializers.DateField(required=False, allow_null=True)
    expiry_date = serializers.DateField(required=False, allow_null=True)
    front_image = serializers.FileField()
    back_image = serializers.FileField(required=False, allow_null=True)
    evidence_type = serializers.ChoiceField(
        choices=GuardianEvidence.EvidenceType.choices, required=False
    )
    evidence_file = serializers.FileField(required=False)

    def validate_nationality(self, value):
        value = value.upper()
        if not re.fullmatch(r"[A-Z]{2}", value):
            raise serializers.ValidationError("Use an ISO alpha-2 country code.")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        value = attrs["date_of_birth"]
        today = timezone.localdate()
        age = (
            today.year
            - value.year
            - ((today.month, today.day) < (value.month, value.day))
        )
        if value > today or age >= 18:
            raise PatientNotMinor()
        if attrs["document_type"] not in {
            IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
            IdentityDocument.DocumentType.BIRTH_DOCUMENT,
        }:
            raise serializers.ValidationError(
                {"document_type": ["A primary minor identity document is required."]}
            )
        if attrs[
            "relationship"
        ] == GuardianRelationship.Relationship.LEGAL_GUARDIAN and not attrs.get(
            "evidence_file"
        ):
            raise RelationshipEvidenceRequired()
        if bool(attrs.get("evidence_type")) != bool(attrs.get("evidence_file")):
            raise serializers.ValidationError(
                {"evidence_file": ["Evidence type and file must be supplied together."]}
            )

        document_fields = {
            "document_type",
            "document_number",
            "national_number",
            "issuing_country",
            "issue_date",
            "expiry_date",
            "front_image",
            "back_image",
        }
        document = IdentityDocumentInputSerializer(
            data={key: attrs[key] for key in document_fields if key in attrs},
            context={"minor_creation": True},
        )
        document.is_valid(raise_exception=True)
        if evidence := attrs.get("evidence_file"):
            IdentityDocumentInputSerializer()._validate_image(evidence)
        return {
            "profile_data": {
                key: attrs[key]
                for key in (
                    "full_name",
                    "date_of_birth",
                    "sex",
                    "nationality",
                    "blood_group",
                )
            },
            "relationship": attrs["relationship"],
            "identity_data": dict(document.validated_data),
            "evidence_type": attrs.get("evidence_type"),
            "evidence_file": attrs.get("evidence_file"),
        }


class GuardianRelationshipSerializer(serializers.ModelSerializer):
    class Meta:
        model = GuardianRelationship
        fields = (
            "uuid",
            "relationship",
            "verification_status",
            "family_number_result",
            "name_evidence_result",
            "evidence_checked_at",
            "evidence_policy_version",
            "active",
            "started_at",
            "verified_at",
            "ended_at",
            "ended_reason",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class GuardianRelationshipChildSerializer(serializers.ModelSerializer):
    class Meta:
        model = PatientProfile
        fields = ("uuid", "digital_id", "full_name")
        read_only_fields = fields


class GuardianRelationshipPatientSerializer(serializers.ModelSerializer):
    minor_patient = GuardianRelationshipChildSerializer(read_only=True)
    status = serializers.SerializerMethodField()
    can_revoke = serializers.SerializerMethodField()

    class Meta:
        model = GuardianRelationship
        fields = (
            "uuid",
            "minor_patient",
            "relationship",
            "status",
            "can_revoke",
            "started_at",
            "verified_at",
            "ended_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    @extend_schema_field(
        serializers.ChoiceField(
            choices=("PENDING", "VERIFIED", "REJECTED", "REVOKED", "UNKNOWN")
        )
    )
    def get_status(self, obj):
        if obj.ended_at is not None:
            return "REVOKED"
        if obj.verification_status == GuardianRelationship.VerificationStatus.REJECTED:
            return "REJECTED"
        if (
            obj.verification_status == GuardianRelationship.VerificationStatus.VERIFIED
            and obj.active
        ):
            return "VERIFIED"
        if obj.verification_status == GuardianRelationship.VerificationStatus.PENDING:
            return "PENDING"
        return "UNKNOWN"

    @extend_schema_field(serializers.BooleanField())
    def get_can_revoke(self, obj):
        return (
            obj.ended_at is None
            and obj.active
            and obj.verification_status
            == GuardianRelationship.VerificationStatus.VERIFIED
        )


class MinorSerializer(serializers.ModelSerializer):
    age = serializers.IntegerField(read_only=True)
    is_minor = serializers.BooleanField(read_only=True)
    relationship = serializers.SerializerMethodField()

    class Meta:
        model = PatientProfile
        fields = (
            "uuid",
            "digital_id",
            "full_name",
            "date_of_birth",
            "age",
            "is_minor",
            "sex",
            "nationality",
            "blood_group",
            "identity_status",
            "relationship",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    @extend_schema_field(GuardianRelationshipSerializer)
    def get_relationship(self, obj):
        relationship = getattr(obj, "authorized_relationship", None)
        if relationship is None:
            relationship = self.context.get("relationship")
        return (
            GuardianRelationshipSerializer(relationship).data if relationship else None
        )


class MinorCreateResponseSerializer(MinorSerializer):
    pass


class GuardianEvidenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = GuardianEvidence
        fields = ("uuid", "evidence_type", "created_at")
        read_only_fields = fields


class GuardianVerificationPatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = PatientProfile
        fields = (
            "uuid",
            "digital_id",
            "full_name",
            "identity_status",
        )
        read_only_fields = fields


class GuardianRelationshipVerificationSerializer(GuardianRelationshipSerializer):
    minor_patient = GuardianVerificationPatientSerializer(read_only=True)
    guardian_patient = serializers.SerializerMethodField()
    evidences = GuardianEvidenceSerializer(many=True, read_only=True)

    class Meta(GuardianRelationshipSerializer.Meta):
        fields = GuardianRelationshipSerializer.Meta.fields + (
            "minor_patient",
            "guardian_patient",
            "evidences",
            "rejection_reason",
        )

    @extend_schema_field(GuardianVerificationPatientSerializer)
    def get_guardian_patient(self, obj):
        profile = getattr(obj.guardian_user, "patient_profile", None)
        return GuardianVerificationPatientSerializer(profile).data if profile else None


class GuardianRelationshipFilterSerializer(
    RejectUnknownFieldsMixin, serializers.Serializer
):
    status = serializers.ChoiceField(
        choices=GuardianRelationship.VerificationStatus.choices, required=False
    )


class EmptySerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    pass


class RelationshipRejectionSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    rejection_reason = serializers.CharField(
        min_length=1, max_length=1000, trim_whitespace=True
    )


class RelationshipRevocationSerializer(
    RejectUnknownFieldsMixin, serializers.Serializer
):
    reason = serializers.CharField(min_length=1, max_length=1000, trim_whitespace=True)
