"""Anonymous password recovery built on the M31 OTP authority."""

import hashlib

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)

from accounts.services import normalize_email
from audit.models import AuditLog
from audit.services import record_audit
from otp.delivery import OtpDeliveryService
from otp.exceptions import OtpDeliveryFailed
from otp.models import OtpAuthorization, OtpPurpose, OtpTargetState
from otp.services import consume_otp_authorization, issue_otp, verify_otp


class _DiscardingDelivery(OtpDeliveryService):
    """Exercise identical OTP persistence/rate controls without sending mail."""

    def send_email_otp(self, *, target, code, expires_minutes, locale="en"):
        return None


def _eligible_account(email):
    user_model = get_user_model()
    try:
        user = user_model.objects.get(email__iexact=normalize_email(email))
    except user_model.DoesNotExist:
        return None
    if (
        not user.is_active
        or user.status != user.Status.ACTIVE
        or not user.email_verified
    ):
        return None
    return user


def request_password_reset(*, email, source=None):
    """Issue real or non-delivered decoy OTP with same public behavior."""
    normalized_email = normalize_email(email)
    account = _eligible_account(normalized_email)
    target = account.email if account is not None else normalized_email
    try:
        result = issue_otp(
            purpose=OtpPurpose.PASSWORD_RESET,
            channel=OtpTargetState.Channel.EMAIL,
            target=target,
            account=account,
            source=source,
            delivery_service=None if account is not None else _DiscardingDelivery(),
        )
    except OtpDeliveryFailed:
        # Provider state must not make account existence observable.
        result = None
    record_audit(
        action=AuditLog.Action.PASSWORD_RESET_OTP_REQUESTED,
        actor=account,
        resource_type="OTP_CHALLENGE" if result is not None else "PASSWORD_RESET",
        resource_uuid=result.challenge_uuid if result is not None else None,
        metadata={"purpose": OtpPurpose.PASSWORD_RESET},
    )
    return result


def verify_password_reset(*, email, code):
    normalized_email = normalize_email(email)
    account = _eligible_account(normalized_email)
    target = account.email if account is not None else normalized_email
    authorization = verify_otp(
        purpose=OtpPurpose.PASSWORD_RESET,
        channel=OtpTargetState.Channel.EMAIL,
        target=target,
        code=code,
        account=account,
    )
    if account is None:
        # Defensive: a decoy challenge can never grant account authority.
        raise ValueError("Password reset account unavailable.")
    record_audit(
        action=AuditLog.Action.PASSWORD_RESET_OTP_VERIFIED,
        actor=account,
        resource_type="USER",
        resource_uuid=account.uuid,
        metadata={"purpose": OtpPurpose.PASSWORD_RESET},
    )
    return authorization


def _authorization_for_update(reset_token):
    token_hash = hashlib.sha256(reset_token.encode()).hexdigest()
    return (
        OtpAuthorization.objects.select_for_update()
        .select_related("challenge__state")
        .get(token_hash=token_hash)
    )


@transaction.atomic
def confirm_password_reset(*, reset_token, new_password):
    """Validate policy, consume capability, change password, revoke sessions."""
    authorization = _authorization_for_update(reset_token)
    account_id = authorization.challenge.account_id
    if account_id is None:
        from otp.exceptions import InvalidOtp

        raise InvalidOtp("Invalid password reset capability.")
    account = get_user_model().objects.select_for_update().get(pk=account_id)
    validate_password(new_password, user=account)
    consume_otp_authorization(
        token=reset_token,
        purpose=OtpPurpose.PASSWORD_RESET,
        channel=OtpTargetState.Channel.EMAIL,
        target=account.email,
        account=account,
    )
    account.set_password(new_password)
    account.auth_session_version += 1
    account.save(update_fields=("password", "auth_session_version", "updated_at"))

    outstanding = OutstandingToken.objects.filter(user=account).exclude(
        blacklistedtoken__isnull=False
    )
    BlacklistedToken.objects.bulk_create(
        [BlacklistedToken(token=token) for token in outstanding],
        ignore_conflicts=True,
    )
    record_audit(
        action=AuditLog.Action.PASSWORD_RESET_COMPLETED,
        actor=account,
        resource_type="USER",
        resource_uuid=account.uuid,
        metadata={"sessions_revoked": True},
    )
    return account
