from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.services import issue_tokens, normalize_email, register_account
from common.exceptions import AccountUnavailable
from patients.models import PatientProfile
from patients.serializers import PatientProfileInputSerializer
from registration.serializers import RegistrationIdentitySerializer

User = get_user_model()


class RejectUnknownFieldsMixin:
    def validate(self, attrs):
        unknown = set(self.initial_data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {field: ["This field is not allowed."] for field in sorted(unknown)}
            )
        return attrs


class PublicUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            "uuid",
            "email",
            "phone",
            "role",
            "email_verified",
            "phone_verified",
            "created_at",
        )
        read_only_fields = fields


class RegisterSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    email = serializers.EmailField()
    phone = serializers.CharField(required=False, allow_blank=True, max_length=32)
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    # Scan-first registration residence (required only for the scan-first
    # path; legacy manual registration keeps its existing contract).
    governorate = serializers.ChoiceField(
        choices=PatientProfile.Governorate.choices, required=False
    )
    # LEGACY manual registration (patient demographics supplied directly).
    patient = PatientProfileInputSerializer(write_only=True, required=False)
    # SCAN-FIRST registration (capability-bound identity session + confirmed
    # human-reviewed demographic values). Exactly one of the two is allowed.
    registration_identity = RegistrationIdentitySerializer(
        write_only=True, required=False
    )

    def validate_email(self, value):
        value = normalize_email(value)
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(
                "An account with this email already exists."
            )
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        has_legacy = "patient" in attrs
        has_scan_first = "registration_identity" in attrs
        if has_legacy == has_scan_first:
            raise serializers.ValidationError(
                "Provide exactly one of 'patient' (manual) or "
                "'registration_identity' (scan-first)."
            )
        if has_scan_first and not attrs.get("governorate"):
            raise serializers.ValidationError(
                {"governorate": ["This field is required for scan-first registration."]}
            )
        try:
            validate_password(attrs["password"], user=User(email=attrs["email"]))
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": exc.messages}) from exc
        return attrs

    def create(self, validated_data):
        return register_account(**validated_data)


class LoginSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        return issue_tokens(**attrs)


def _get_available_user(refresh):
    user_id = refresh.payload.get(api_settings.USER_ID_CLAIM)
    try:
        user = User.objects.get(**{api_settings.USER_ID_FIELD: user_id})
    except (User.DoesNotExist, ValueError, TypeError) as exc:
        raise AccountUnavailable() from exc
    if not user.is_active or user.status != user.Status.ACTIVE:
        raise AccountUnavailable()
    return user


class RefreshSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    refresh = serializers.CharField(write_only=True)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        try:
            refresh = RefreshToken(attrs["refresh"])
            _get_available_user(refresh)
            access = str(refresh.access_token)
            refresh.blacklist()
            refresh.set_jti()
            refresh.set_exp()
            refresh.set_iat()
            refresh.outstand()
        except TokenError as exc:
            raise InvalidToken(str(exc)) from exc
        return {"access": access, "refresh": str(refresh)}


class LogoutSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    refresh = serializers.CharField(write_only=True)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        try:
            refresh = RefreshToken(attrs["refresh"])
            token_user = _get_available_user(refresh)
            if token_user.pk != self.context["request"].user.pk:
                raise InvalidToken("Token does not belong to this account.")
            refresh.blacklist()
        except TokenError as exc:
            raise InvalidToken(str(exc)) from exc
        return attrs


class TokenPairSerializer(serializers.Serializer):
    access = serializers.CharField(read_only=True)
    refresh = serializers.CharField(read_only=True)


class MessageSerializer(serializers.Serializer):
    message = serializers.CharField(read_only=True)


class ErrorBodySerializer(serializers.Serializer):
    code = serializers.CharField(read_only=True)
    message = serializers.CharField(read_only=True)
    details = serializers.DictField(read_only=True)


class ErrorEnvelopeSerializer(serializers.Serializer):
    error = ErrorBodySerializer(read_only=True)
