from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.password_reset import (
    confirm_password_reset,
    request_password_reset,
    verify_password_reset,
)
from accounts.serializers import ErrorEnvelopeSerializer, RejectUnknownFieldsMixin
from accounts.throttles import (
    PasswordResetConfirmRateThrottle,
    PasswordResetRequestRateThrottle,
    PasswordResetVerifyRateThrottle,
)
from otp.exceptions import InvalidOtp, OtpCooldown, OtpRateLimited
from otp.models import OtpAuthorization

GENERIC_MESSAGE = "If an eligible account exists, a verification code has been sent."


def envelope(name, child):
    return inline_serializer(name=name, fields={"data": child})


class PasswordResetRequestSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetVerifySerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.RegexField(r"^\d{6}$", trim_whitespace=True)


class PasswordResetConfirmSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    reset_token = serializers.CharField(
        write_only=True, max_length=256, trim_whitespace=False
    )
    new_password = serializers.CharField(
        write_only=True, max_length=256, trim_whitespace=False
    )


class PasswordResetOtpInvalid(APIException):
    status_code = 400
    default_code = "password_reset_otp_invalid"
    default_detail = "The verification code is invalid or unavailable."


class PasswordResetCapabilityInvalid(APIException):
    status_code = 400
    default_code = "password_reset_capability_invalid"
    default_detail = "The password reset session is invalid or expired."


def _throttled_response(retry_after=None):
    details = {}
    if retry_after is not None:
        details["retry_after"] = int(retry_after)
    return Response(
        {
            "error": {
                "code": "throttled",
                "message": "Too many attempts. Please try again shortly.",
                "details": details,
            }
        },
        status=status.HTTP_429_TOO_MANY_REQUESTS,
    )


class PasswordResetRequestView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [PasswordResetRequestRateThrottle]
    throttle_scope = "password_reset_request"

    @extend_schema(
        operation_id="password_reset_request",
        request=PasswordResetRequestSerializer,
        responses={
            200: envelope(
                "PasswordResetRequested",
                inline_serializer(
                    name="PasswordResetRequestedData",
                    fields={
                        "message": serializers.CharField(),
                        "resend_after_seconds": serializers.IntegerField(),
                    },
                ),
            ),
            400: ErrorEnvelopeSerializer,
            429: ErrorEnvelopeSerializer,
        },
        tags=["Authentication"],
    )
    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            request_password_reset(
                email=serializer.validated_data["email"],
                source=request.META.get("REMOTE_ADDR") or None,
            )
        except OtpCooldown as exc:
            return _throttled_response(exc.retry_after_seconds)
        except OtpRateLimited:
            return _throttled_response()
        return Response(
            {
                "data": {
                    "message": GENERIC_MESSAGE,
                    "resend_after_seconds": settings.OTP_RESEND_COOLDOWN_SECONDS,
                }
            }
        )


class PasswordResetVerifyView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [PasswordResetVerifyRateThrottle]
    throttle_scope = "password_reset_verify"

    @extend_schema(
        operation_id="password_reset_verify",
        request=PasswordResetVerifySerializer,
        responses={
            200: envelope(
                "PasswordResetVerified",
                inline_serializer(
                    name="PasswordResetVerifiedData",
                    fields={
                        "reset_token": serializers.CharField(),
                        "expires_at": serializers.DateTimeField(),
                    },
                ),
            ),
            400: ErrorEnvelopeSerializer,
            429: ErrorEnvelopeSerializer,
        },
        tags=["Authentication"],
    )
    def post(self, request):
        serializer = PasswordResetVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = verify_password_reset(**serializer.validated_data)
        except (InvalidOtp, ValueError) as exc:
            raise PasswordResetOtpInvalid() from exc
        return Response(
            {
                "data": {
                    "reset_token": result.token,
                    "expires_at": result.expires_at.isoformat(),
                }
            }
        )


class PasswordResetConfirmView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [PasswordResetConfirmRateThrottle]
    throttle_scope = "password_reset_confirm"

    @extend_schema(
        operation_id="password_reset_confirm",
        request=PasswordResetConfirmSerializer,
        responses={
            200: envelope(
                "PasswordResetConfirmed",
                inline_serializer(
                    name="PasswordResetConfirmedData",
                    fields={"message": serializers.CharField()},
                ),
            ),
            400: ErrorEnvelopeSerializer,
            429: ErrorEnvelopeSerializer,
        },
        tags=["Authentication"],
    )
    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            confirm_password_reset(**serializer.validated_data)
        except (InvalidOtp, OtpAuthorization.DoesNotExist) as exc:
            raise PasswordResetCapabilityInvalid() from exc
        except DjangoValidationError as exc:
            raise ValidationError({"new_password": exc.messages}) from exc
        return Response(
            {"data": {"message": "Password reset completed. Sign in again."}}
        )
