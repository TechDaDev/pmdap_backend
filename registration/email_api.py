"""Public M31B registration email-verification endpoints (anonymous, throttled).

Purpose and OTP target are ALWAYS chosen server-side from the registration
session — never client-controlled. OTP codes are never logged. A session
capability (``session_token``) is sent in the body for POSTs and in the
``X-Registration-Session-Token`` header for the GET status endpoint.
"""
import logging

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import ErrorEnvelopeSerializer
from otp.exceptions import (
    InvalidOtp,
    OtpCooldown,
    OtpDeliveryFailed,
    OtpProviderError,
    OtpRateLimited,
)
from registration.email_services import (
    get_registration_status,
    mask_email,
    resend_registration_otp,
    start_registration_session,
    verify_registration_otp,
)
from registration.serializers import (
    RegistrationEmailResendSerializer,
    RegistrationEmailStartSerializer,
    RegistrationEmailVerifySerializer,
)
from registration.throttles import (
    RegistrationEmailResendRateThrottle,
    RegistrationEmailStartRateThrottle,
    RegistrationEmailStatusRateThrottle,
    RegistrationEmailVerifyRateThrottle,
)

logger = logging.getLogger(__name__)


def envelope(name, child):
    return inline_serializer(name=name, fields={"data": child})


EMAIL_STATUS_DATA = inline_serializer(
    name="RegistrationEmailStatusData",
    fields={
        "session_id": serializers.UUIDField(),
        "masked_email": serializers.CharField(),
        "status": serializers.CharField(),
        "email_verified": serializers.BooleanField(),
        "resend_at": serializers.CharField(required=False, allow_null=True),
        "expires_at": serializers.CharField(required=False, allow_null=True),
    },
)

ERRORS = {400: ErrorEnvelopeSerializer, 429: ErrorEnvelopeSerializer}


def _throttled_response(retry_after=None):
    """429 error envelope with a machine-readable retry window."""
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


def _log_delivery_cause(exc):
    """Temporary M31B diagnostics: log the chained provider/SMTP error.

    Logs exception type + message only — never the OTP code or target.
    """
    cause = getattr(exc, "__cause__", None)
    logger.warning(
        "registration OTP delivery failed: %r",
        repr(cause) if cause is not None else repr(exc),
    )


class RegistrationEmailStartView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [RegistrationEmailStartRateThrottle]
    throttle_scope = "registration_email_start"

    @extend_schema(
        operation_id="registration_email_start",
        request=RegistrationEmailStartSerializer,
        responses={
            201: envelope(
                "RegistrationEmailStartCreated",
                inline_serializer(
                    name="RegistrationEmailStartCreatedData",
                    fields={
                        "session_id": serializers.UUIDField(),
                        "session_token": serializers.CharField(),
                        "masked_email": serializers.CharField(),
                        "status": serializers.CharField(),
                        "resend_at": serializers.CharField(
                            required=False, allow_null=True
                        ),
                        "expires_at": serializers.CharField(
                            required=False, allow_null=True
                        ),
                    },
                ),
            ),
            **ERRORS,
            503: ErrorEnvelopeSerializer,
        },
        tags=["Registration"],
        description=(
            "Account-details step. Creates a capability-bound registration "
            "session and sends a 6-digit email-verification OTP to the email. "
            "Returns the session_token exactly once (only its digest is "
            "stored). The purpose and target are chosen server-side."
        ),
    )
    def post(self, request):
        serializer = RegistrationEmailStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            session, token = start_registration_session(
                email=serializer.validated_data["email"],
                phone=serializer.validated_data.get("phone", ""),
                governorate=serializer.validated_data.get("governorate", ""),
                request=request,
            )
        except OtpCooldown as exc:
            return _throttled_response(exc.retry_after_seconds)
        except OtpRateLimited:
            return _throttled_response()
        except (OtpDeliveryFailed, OtpProviderError) as exc:
            _log_delivery_cause(exc)
            raise _DeliveryUnavailable() from exc
        return Response(
            {
                "data": {
                    "session_id": str(session.uuid),
                    "session_token": token,
                    "masked_email": mask_email(session.email),
                    "status": session.status,
                    "resend_at": None,
                    "expires_at": (
                        session.expires_at.isoformat()
                        if session.expires_at
                        else None
                    ),
                }
            },
            status=status.HTTP_201_CREATED,
        )


class RegistrationEmailResendView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [RegistrationEmailResendRateThrottle]
    throttle_scope = "registration_email_resend"

    @extend_schema(
        operation_id="registration_email_resend",
        request=RegistrationEmailResendSerializer,
        responses={
            200: envelope("RegistrationEmailResent", EMAIL_STATUS_DATA),
            **ERRORS,
            503: ErrorEnvelopeSerializer,
        },
        tags=["Registration"],
        description=(
            "Resend the email-verification OTP. Cooldown and issuance limits "
            "come from the OTP core; resending invalidates the previous code."
        ),
    )
    def post(self, request):
        serializer = RegistrationEmailResendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            session, result = resend_registration_otp(
                session_token=serializer.validated_data["session_token"],
                request=request,
            )
        except OtpCooldown as exc:
            return _throttled_response(exc.retry_after_seconds)
        except OtpRateLimited:
            return _throttled_response()
        except (OtpDeliveryFailed, OtpProviderError) as exc:
            _log_delivery_cause(exc)
            raise _DeliveryUnavailable() from exc
        status_data = get_registration_status(
            session_token=serializer.validated_data["session_token"]
        )
        status_data["resend_at"] = result.resend_at.isoformat()
        return Response({"data": status_data})


class RegistrationEmailVerifyView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [RegistrationEmailVerifyRateThrottle]
    throttle_scope = "registration_email_verify"

    @extend_schema(
        operation_id="registration_email_verify",
        request=RegistrationEmailVerifySerializer,
        responses={
            200: envelope("RegistrationEmailVerified", EMAIL_STATUS_DATA),
            **ERRORS,
            503: ErrorEnvelopeSerializer,
        },
        tags=["Registration"],
        description=(
            "Verify the email-verification OTP for the session's own email "
            "and mark the registration email verified server-side. The code "
            "is one-time: replay is denied."
        ),
    )
    def post(self, request):
        serializer = RegistrationEmailVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            session = verify_registration_otp(
                session_token=serializer.validated_data["session_token"],
                code=serializer.validated_data["code"],
            )
        except InvalidOtp as exc:
            raise ValidationError(
                {"code": ["The verification code is invalid or has expired."]}
            ) from exc
        status_data = get_registration_status(
            session_token=serializer.validated_data["session_token"]
        )
        status_data["email_verified_at"] = (
            session.email_verified_at.isoformat()
            if session.email_verified_at
            else None
        )
        return Response({"data": status_data})


class RegistrationEmailStatusView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [RegistrationEmailStatusRateThrottle]
    throttle_scope = "registration_email_status"

    @extend_schema(
        operation_id="registration_email_status",
        parameters=[],
        responses={
            200: envelope("RegistrationEmailStatus", EMAIL_STATUS_DATA),
            404: ErrorEnvelopeSerializer,
            429: ErrorEnvelopeSerializer,
        },
        tags=["Registration"],
        description=(
            "Resume endpoint. The session capability is sent in the "
            "X-Registration-Session-Token header (never in the URL). Returns "
            "the masked email, verification state, and resend/expiry windows."
        ),
    )
    def get(self, request):
        token = request.headers.get("X-Registration-Session-Token") or ""
        return Response({"data": get_registration_status(session_token=token)})


class _DeliveryUnavailable(APIException):
    """Delivery failures map to a generic 503 (no provider details leaked)."""

    status_code = 503
    default_code = "registration_email_delivery_failed"
    default_detail = (
        "The verification email could not be sent. Please try again."
    )
