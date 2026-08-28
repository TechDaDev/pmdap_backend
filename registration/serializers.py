"""Serializers for the scan-first registration identity flow.

Advisory only: extraction values are suggestions. The human-reviewed values in
``RegistrationIdentitySerializer`` are authoritative for profile creation.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from identities.models import IdentityDocument
from identities.services import inspect_identity_upload
from patients.models import PatientProfile
from patients.serializers import PatientProfileInputSerializer
from registration.models import RegistrationIdentityExtractionJob


class RejectUnknownFieldsMixin:
    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {field: ["This field is not allowed."] for field in sorted(unknown)}
            )
        return super().to_internal_value(data)


class RegistrationIdentityExtractRequestSerializer(
    RejectUnknownFieldsMixin, serializers.Serializer
):
    """Public (anonymous) scan-first extraction request.

    Phase 1 restricts scan-first registration to the Iraqi Unified National
    Card. Front + back images are required and validated with the same strict
    rules as authenticated identity uploads.
    """

    document_type = serializers.ChoiceField(
        choices=(IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,)
    )
    front_image = serializers.ImageField()
    back_image = serializers.ImageField()

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


class RegistrationExtractionFieldSerializer(serializers.Serializer):
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


class RegistrationExtractionMrzSerializer(serializers.Serializer):
    detected = serializers.BooleanField()
    valid = serializers.BooleanField()
    checks_passed = serializers.BooleanField()


class RegistrationIdentityStatusSerializer(serializers.Serializer):
    job_id = serializers.UUIDField()
    status = serializers.ChoiceField(
        choices=RegistrationIdentityExtractionJob.Status.choices
    )
    error_code = serializers.CharField(required=False, allow_blank=True, default="")
    fields = serializers.DictField(
        child=RegistrationExtractionFieldSerializer(), required=False
    )
    warnings = serializers.ListField(child=serializers.CharField(), required=False)
    mrz = RegistrationExtractionMrzSerializer(required=False)


class RegistrationIdentitySerializer(
    RejectUnknownFieldsMixin, serializers.Serializer
):
    """Confirmed, human-reviewed identity data for scan-first registration.

    ``job_id`` + ``job_token`` are the capability for the pre-registration
    extraction session; the four identifiers remain distinct and are never
    mapped into one another.
    """

    job_id = serializers.UUIDField()
    job_token = serializers.CharField(max_length=128, trim_whitespace=False)
    document_type = serializers.ChoiceField(
        choices=(IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,)
    )
    document_number = serializers.CharField(max_length=128)
    national_card_number = serializers.CharField(
        max_length=128, required=False, allow_blank=True, default=""
    )
    family_number = serializers.CharField(
        max_length=128, required=False, allow_blank=True, default=""
    )
    unique_card_body_number = serializers.CharField(
        max_length=128, required=False, allow_blank=True, default=""
    )
    issue_date = serializers.DateField(required=False, allow_null=True)
    expiry_date = serializers.DateField(required=False, allow_null=True)
    # Structured patronymic components (Arabic names are natural input; only
    # whitespace is normalized). `full_name` is derived server-side.
    name = serializers.CharField(max_length=255)
    father_name = serializers.CharField(max_length=255)
    grandfather_name = serializers.CharField(max_length=255)
    # Authoritative confirmed mother's given name (National Card maternal
    # field). Optional at registration; required for MOTHER evidence later.
    mother_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )
    # Explicit human acknowledgement that the confirmed values match the card.
    confirmation = serializers.BooleanField()
    date_of_birth = serializers.DateField()
    sex = serializers.ChoiceField(choices=PatientProfile.Sex.choices)
    nationality = serializers.CharField(min_length=2, max_length=2)
    blood_group = serializers.ChoiceField(
        choices=PatientProfile.BloodGroup.choices,
        required=False,
        default=PatientProfile.BloodGroup.UNKNOWN,
    )

    def _normalize_name(self, value):
        return " ".join(value.split())

    def validate_name(self, value):
        value = self._normalize_name(value)
        if not value:
            raise serializers.ValidationError("Name is required.")
        return value

    def validate_father_name(self, value):
        value = self._normalize_name(value)
        if not value:
            raise serializers.ValidationError("Father's name is required.")
        return value

    def validate_grandfather_name(self, value):
        value = self._normalize_name(value)
        if not value:
            raise serializers.ValidationError("Grandfather's name is required.")
        return value

    def validate_confirmation(self, value):
        if value is not True:
            raise serializers.ValidationError(
                "You must confirm the information matches your National Card."
            )
        return value

    def validate_date_of_birth(self, value):
        # Direct ownership requires an adult patient; the HUMAN-CONFIRMED DOB
        # is authoritative here.
        return PatientProfileInputSerializer().validate_date_of_birth(value)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        issue = attrs.get("issue_date")
        expiry = attrs.get("expiry_date")
        if issue and expiry and expiry <= issue:
            raise serializers.ValidationError(
                {"expiry_date": ["Expiry date must be after issue date."]}
            )
        body = attrs.get("unique_card_body_number", "").strip()
        supplied = attrs.get("document_number", "").strip()
        national = attrs.get("national_card_number", "").strip()
        if not body and supplied != national:
            body = supplied
            attrs["unique_card_body_number"] = body
        attrs["document_number"] = body
        return attrs

    def validate_nationality(self, value):
        return PatientProfileInputSerializer().validate_nationality(value)
