"""Authenticated M32B password-change endpoints.

All three views require a valid Bearer session (ActiveAccountJWTAuthentication
+ IsAuthenticated). The acting user's account is authoritative: the OTP target
and the change-capability binding are always derived from ``request.user``,
never from a client-chosen target.
"""

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.password_change import (
    EmailNotVerified,
    WrongCurrentPassword,
    confirm_password_change,
    request_password_change_otp,
    verify_password_change_otp,
)
from accounts.serializers import ErrorEnvelopeSerializer, RejectUnknownFieldsMixin
from accounts.throttles import (
    PasswordChangeConfirmRateThrottle,
    PasswordChangeRequestRateThrottle,
    PasswordChangeVerifyRateThrottle,
)
from otp.exceptions import InvalidOtp, OtpCooldown, OtpRateLimited
from otp.models import OtpAuthorization


def envelope(name, child):
    return inline_serializer(name=name, fields={"data": child})


class PasswordChangeRequestSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    current_password = serializers.CharField(
        write_only=True, max_length=256, trim_whitespace=False
    )


class PasswordChangeVerifySerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    code = serializers.RegexField(r"^\d{6}$", trim_whitespace=True)


class PasswordChangeConfirmSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    capability = serializers.CharField(
        write_only=True, max_length=256, trim_whitespace=False
    )
    new_password = serializers.CharField(
        write_only=True, max_length=256, trim_whitespace=False
    )


class PasswordChangeOtpInvalid(APIException):
    status_code = 400
    default_code = "password_change_otp_invalid"
    default_detail = "The verification code is invalid or unavailable."


class PasswordChangeCapabilityInvalid(APIException):
    status_code = 400
    default_code = "password_change_capability_invalid"
    default_detail = "The password change session is invalid or expired."


class PasswordChangeWrongCurrentPassword(APIException):
    status_code = 400
    default_code = "password_change_wrong_current_password"
    default_detail = "The current password is incorrect."


class PasswordChangeEmailUnverified(APIException):
    status_code = 400
    default_code = "password_change_email_unverified"
    default_detail = "Verify your email before changing your password."


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


class PasswordChangeRequestView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [PasswordChangeRequestRateThrottle]
    throttle_scope = "password_change_request"

    @extend_schema(
        operation_id="password_change_request",
        request=PasswordChangeRequestSerializer,
        responses={
            200: envelope(
                "PasswordChangeRequested",
                inline_serializer(
                    name="PasswordChangeRequestedData",
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
        description=(
            "Authenticated password change: requires the current password. "
            "Sends a PASSWORD_CHANGE OTP to the account's verified email."
        ),
    )
    def post(self, request):
        serializer = PasswordChangeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            request_password_change_otp(
                user=request.user,
                current_password=serializer.validated_data["current_password"],
                source=request.META.get("REMOTE_ADDR") or None,
            )
        except WrongCurrentPassword as exc:
            raise PasswordChangeWrongCurrentPassword() from exc
        except EmailNotVerified as exc:
            raise PasswordChangeEmailUnverified() from exc
        except OtpCooldown as exc:
            return _throttled_response(exc.retry_after_seconds)
        except OtpRateLimited:
            return _throttled_response()
        return Response(
            {
                "data": {
                    "message": "A verification code has been sent to your email.",
                    "resend_after_seconds": settings.OTP_RESEND_COOLDOWN_SECONDS,
                }
            }
        )


class PasswordChangeVerifyView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [PasswordChangeVerifyRateThrottle]
    throttle_scope = "password_change_verify"

    @extend_schema(
        operation_id="password_change_verify",
        request=PasswordChangeVerifySerializer,
        responses={
            200: envelope(
                "PasswordChangeVerified",
                inline_serializer(
                    name="PasswordChangeVerifiedData",
                    fields={
                        "capability": serializers.CharField(),
                        "expires_at": serializers.DateTimeField(),
                    },
                ),
            ),
            400: ErrorEnvelopeSerializer,
            429: ErrorEnvelopeSerializer,
        },
        tags=["Authentication"],
        description=(
            "Verify the PASSWORD_CHANGE OTP and obtain a short-lived, "
            "single-use, user-bound change capability."
        ),
    )
    def post(self, request):
        serializer = PasswordChangeVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = verify_password_change_otp(
                user=request.user,
                code=serializer.validated_data["code"],
            )
        except InvalidOtp as exc:
            raise PasswordChangeOtpInvalid() from exc
        return Response(
            {
                "data": {
                    "capability": result.token,
                    "expires_at": result.expires_at.isoformat(),
                }
            }
        )


class PasswordChangeConfirmView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [PasswordChangeConfirmRateThrottle]
    throttle_scope = "password_change_confirm"

    @extend_schema(
        operation_id="password_change_confirm",
        request=PasswordChangeConfirmSerializer,
        responses={
            200: envelope(
                "PasswordChangeConfirmed",
                inline_serializer(
                    name="PasswordChangeConfirmedData",
                    fields={
                        "message": serializers.CharField(),
                        "access": serializers.CharField(),
                        "refresh": serializers.CharField(),
                    },
                ),
            ),
            400: ErrorEnvelopeSerializer,
            429: ErrorEnvelopeSerializer,
        },
        tags=["Authentication"],
        description=(
            "Consume the change capability and set the new password. All other "
            "sessions are revoked; the acting session receives a fresh token "
            "pair so the current device stays logged in."
        ),
    )
    def post(self, request):
        serializer = PasswordChangeConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            tokens = confirm_password_change(
                user=request.user,
                capability=serializer.validated_data["capability"],
                new_password=serializer.validated_data["new_password"],
            )
        except (InvalidOtp, OtpAuthorization.DoesNotExist) as exc:
            raise PasswordChangeCapabilityInvalid() from exc
        except DjangoValidationError as exc:
            raise ValidationError({"new_password": exc.messages}) from exc
        return Response(
            {
                "data": {
                    "message": "Password changed.",
                    "access": tokens["access"],
                    "refresh": tokens["refresh"],
                }
            }
        )
