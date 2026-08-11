import re

from django.utils import timezone
from rest_framework import serializers

from patients.models import PatientProfile

IDENTITY_FIELDS = {"full_name", "date_of_birth", "sex", "nationality"}


class RejectUnknownFieldsMixin:
    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {field: ["This field is not allowed."] for field in sorted(unknown)}
            )
        return super().to_internal_value(data)


class PatientProfileInputSerializer(
    RejectUnknownFieldsMixin, serializers.ModelSerializer
):
    nationality = serializers.CharField(min_length=2, max_length=2)
    avatar = serializers.ImageField(required=False, allow_null=True)

    class Meta:
        model = PatientProfile
        fields = (
            "full_name",
            "date_of_birth",
            "sex",
            "nationality",
            "blood_group",
            "avatar",
        )

    def validate_date_of_birth(self, value):
        today = timezone.localdate()
        age = (
            today.year
            - value.year
            - ((today.month, today.day) < (value.month, value.day))
        )
        if age < 18:
            raise serializers.ValidationError(
                "Direct account ownership requires an adult patient."
            )
        return value

    def validate_nationality(self, value):
        value = value.upper()
        if not re.fullmatch(r"[A-Z]{2}", value):
            raise serializers.ValidationError("Use an ISO alpha-2 country code.")
        return value


class PatientProfileUpdateSerializer(
    RejectUnknownFieldsMixin, serializers.ModelSerializer
):
    nationality = serializers.CharField(required=False, min_length=2, max_length=2)
    avatar = serializers.ImageField(required=False, allow_null=True)

    class Meta:
        model = PatientProfile
        fields = (
            "full_name",
            "date_of_birth",
            "sex",
            "nationality",
            "blood_group",
            "avatar",
        )
        extra_kwargs = {field: {"required": False} for field in fields}

    def validate_date_of_birth(self, value):
        return PatientProfileInputSerializer().validate_date_of_birth(value)

    def validate_nationality(self, value):
        return PatientProfileInputSerializer().validate_nationality(value)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance.identity_status == PatientProfile.IdentityStatus.VERIFIED:
            locked = IDENTITY_FIELDS.intersection(attrs)
            if locked:
                raise serializers.ValidationError(
                    {
                        field: ["Verified identity fields require controlled review."]
                        for field in sorted(locked)
                    }
                )
        return attrs


class PatientProfileSerializer(serializers.ModelSerializer):
    age = serializers.IntegerField(read_only=True)
    is_minor = serializers.BooleanField(read_only=True)
    avatar_url = serializers.SerializerMethodField(read_only=True)

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
            "avatar_url",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_avatar_url(self, obj) -> str | None:
        if not obj.avatar:
            return None
        return "/api/v1/patients/me/avatar/"
