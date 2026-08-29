"""Service layer for M31B pre-registration email verification.

Reuses the M31A OTP core (issue_otp / verify_otp) with the purpose and target
always chosen server-side from the registration session — never from the
client. The session stores only a SHA-256 capability digest of the
high-entropy ``session_token``; the raw token is returned to the client once.
OTP codes are never logged or stored here.
"""
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from audit.models import AuditLog
from audit.services import record_audit
from otp.delivery import DjangoOtpDeliveryService, ResendOtpDeliveryService
from otp.exceptions import OtpError
from otp.models import OtpPurpose, OtpTargetState
from otp.services import issue_otp, verify_otp
from registration.exceptions import (
    RegistrationEmailAlreadyVerified,
    RegistrationSessionConflict,
    RegistrationSessionExpired,
    RegistrationSessionNotFound,
)
from registration.models import RegistrationSession


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _constant_time_equal(left: str, right: str) -> bool:
    return secrets.compare_digest(left, right)


def mask_email(email: str) -> str:
    """Mask the local part, keeping the first char and the domain.

    ``owner@example.com`` -> ``o***r@example.com``. Never reveals the address.
    """
    local, _, domain = email.partition("@")
    if not local:
        return email
    if len(local) == 1:
        masked_local = f"{local}***"
    else:
        masked_local = f"{local[0]}***{local[-1]}"
    return f"{masked_local}@{domain}"


def get_otp_delivery_service():
    """Resend when configured, otherwise Django's configured email backend."""
    if getattr(settings, "RESEND_API_KEY", ""):
        return ResendOtpDeliveryService()
    return DjangoOtpDeliveryService()


def _client_source(request):
    if request is None:
        return None
    return request.META.get("REMOTE_ADDR") or None


def load_session_for_capability(token: str) -> RegistrationSession:
    """Capability-checked session lookup. Wrong/missing token -> 404."""
    if not token:
        raise RegistrationSessionNotFound()
    try:
        session = RegistrationSession.objects.get(
            capability_digest=_digest(token)
        )
    except RegistrationSession.DoesNotExist:
        raise RegistrationSessionNotFound() from None
    if not _constant_time_equal(_digest(token), session.capability_digest):
        raise RegistrationSessionNotFound()
    return session


def _ensure_active(session):
    """Map session lifecycle to safe domain errors."""
    if session.status == RegistrationSession.Status.FINALIZED:
        raise RegistrationSessionConflict()
    if session.status == RegistrationSession.Status.EXPIRED or (
        session.expires_at and session.expires_at <= timezone.now()
    ):
        if session.status != RegistrationSession.Status.EXPIRED:
            session.status = RegistrationSession.Status.EXPIRED
            session.save(update_fields=("status", "updated_at"))
        raise RegistrationSessionExpired()
    return session


def start_registration_session(*, email, phone="", governorate="", request=None):
    """Create the capability-bound session and issue the first OTP.

    On any OTP failure the session is rolled back so a retry starts clean.
    Returns (session, session_token); the token is returned to the client
    exactly once.
    """
    token = secrets.token_urlsafe(48)
    session = RegistrationSession.objects.create(
        capability_digest=_digest(token),
        email=email,
        phone=phone,
        governorate=governorate,
        expires_at=timezone.now()
        + timedelta(seconds=settings.REGISTRATION_SESSION_TTL_SECONDS),
    )
    try:
        _issue_otp(session, request)
    except OtpError:
        RegistrationSession.objects.filter(pk=session.pk).delete()
        raise
    record_audit(
        action=AuditLog.Action.REGISTRATION_SESSION_CREATED,
        resource_type="REGISTRATION_SESSION",
        resource_uuid=session.uuid,
        metadata={"status": session.status},
    )
    return session, token


def _issue_otp(session, request):
    return issue_otp(
        purpose=OtpPurpose.EMAIL_VERIFICATION,
        channel=OtpTargetState.Channel.EMAIL,
        target=session.email,
        source=_client_source(request),
        delivery_service=get_otp_delivery_service(),
    )


def resend_registration_otp(*, session_token, request=None):
    """Re-issue the email-verification OTP (core enforces cooldown/limits)."""
    session = load_session_for_capability(session_token)
    _ensure_active(session)
    if session.status != RegistrationSession.Status.PENDING_EMAIL_VERIFICATION:
        raise RegistrationEmailAlreadyVerified()
    result = _issue_otp(session, request)
    return session, result


def verify_registration_otp(*, session_token, code):
    """Verify the OTP against the session's own email and mark it verified.

    Strictly non-idempotent: the OTP core consumes the challenge, so replaying
    a used code is denied (InvalidOtp). The authoritative flag is the session
    row, set only here, server-side.
    """
    session = load_session_for_capability(session_token)
    _ensure_active(session)
    if session.status == RegistrationSession.Status.EMAIL_VERIFIED:
        # Replay or repeat attempt after success: the challenge is already
        # consumed, so verification must fail rather than re-grant.
        from otp.exceptions import InvalidOtp

        raise InvalidOtp("Invalid or unavailable OTP.")
    verify_otp(
        purpose=OtpPurpose.EMAIL_VERIFICATION,
        channel=OtpTargetState.Channel.EMAIL,
        target=session.email,
        code=code,
    )
    now = timezone.now()
    session.status = RegistrationSession.Status.EMAIL_VERIFIED
    session.email_verified_at = now
    session.save(update_fields=("status", "email_verified_at", "updated_at"))
    record_audit(
        action=AuditLog.Action.REGISTRATION_EMAIL_VERIFIED,
        resource_type="REGISTRATION_SESSION",
        resource_uuid=session.uuid,
        metadata={"status": session.status},
    )
    return session


def get_registration_status(*, session_token):
    """Resume endpoint payload: masked email, state, resend/expiry windows."""
    session = load_session_for_capability(session_token)
    resend_at = None
    state = OtpTargetState.objects.filter(
        purpose=OtpPurpose.EMAIL_VERIFICATION,
        channel=OtpTargetState.Channel.EMAIL,
    ).first()
    if state is not None and state.last_issued_at is not None:
        resend_at = state.last_issued_at + timedelta(
            seconds=settings.OTP_RESEND_COOLDOWN_SECONDS
        )
    return {
        "session_id": str(session.uuid),
        "masked_email": mask_email(session.email),
        "status": session.status,
        "email_verified": session.status
        == RegistrationSession.Status.EMAIL_VERIFIED,
        "resend_at": resend_at.isoformat() if resend_at else None,
        "expires_at": (
            session.expires_at.isoformat() if session.expires_at else None
        ),
    }


def check_session_for_finalize(*, session_token, email):
    """Final-registration gate. Returns the session or raises a domain error.

    Requires a verified, unexpired session whose email matches the account
    being registered. Called inside the final registration transaction.
    """
    session = load_session_for_capability(session_token)
    _ensure_active(session)
    if session.status != RegistrationSession.Status.EMAIL_VERIFIED:
        from registration.exceptions import RegistrationEmailNotVerified

        raise RegistrationEmailNotVerified()
    if session.email.casefold() != (email or "").casefold():
        from rest_framework import serializers

        raise serializers.ValidationError(
            {"email": ["Email does not match the verified registration email."]}
        )
    return session
