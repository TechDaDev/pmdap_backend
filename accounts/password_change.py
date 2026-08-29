"""Authenticated password change built on the M31 OTP authority.

Not the forgot-password flow. The acting user proves control of the account
with their current password, proves control of the verified email via a
PASSWORD_CHANGE OTP, then sets a new password. The OTP is never the final
change credential — verification yields a short-lived, single-use, user-bound
change capability, and only that capability authorizes the change.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)

from accounts.services import issue_tokens_for_user
from audit.models import AuditLog
from audit.services import record_audit
from otp.delivery import get_otp_delivery_service
from otp.models import OtpPurpose, OtpTargetState
from otp.services import consume_otp_authorization, issue_otp, verify_otp


class WrongCurrentPassword(Exception):
    """Current-password check failed (normal password hasher)."""


class EmailNotVerified(Exception):
    """Password change requires an authoritative verified account email."""


def request_password_change_otp(*, user, current_password, source=None):
    """Prove current password, then issue a PASSWORD_CHANGE OTP.

    The OTP is sent only to the account's authoritative verified email, never
    to a client-chosen target.
    """
    if not user.check_password(current_password):
        raise WrongCurrentPassword()
    if not user.email_verified:
        raise EmailNotVerified()
    result = issue_otp(
        purpose=OtpPurpose.PASSWORD_CHANGE,
        channel=OtpTargetState.Channel.EMAIL,
        target=user.email,
        account=user,
        source=source,
        delivery_service=get_otp_delivery_service(),
    )
    record_audit(
        action=AuditLog.Action.PASSWORD_CHANGE_OTP_REQUESTED,
        actor=user,
        resource_type="OTP_CHALLENGE",
        resource_uuid=result.challenge_uuid,
        metadata={"purpose": OtpPurpose.PASSWORD_CHANGE},
    )
    return result


def verify_password_change_otp(*, user, code):
    """Verify the OTP against the account's own email.

    The OTP core binds the challenge to ``user`` (cross-user denial) and
    consumes it on success (replay denial). Returns the short-lived change
    capability.
    """
    authorization = verify_otp(
        purpose=OtpPurpose.PASSWORD_CHANGE,
        channel=OtpTargetState.Channel.EMAIL,
        target=user.email,
        code=code,
        account=user,
    )
    record_audit(
        action=AuditLog.Action.PASSWORD_CHANGE_OTP_VERIFIED,
        actor=user,
        resource_type="USER",
        resource_uuid=user.uuid,
        metadata={"purpose": OtpPurpose.PASSWORD_CHANGE},
    )
    return authorization


@transaction.atomic
def confirm_password_change(*, user, capability, new_password):
    """Validate policy, consume the capability, change password, rotate session.

    Session policy: revoke ALL other sessions/refresh tokens and issue the
    acting user a fresh token pair (with the new session version) so the
    current device stays logged in; every other device is logged out.
    """
    validate_password(new_password, user=user)
    consume_otp_authorization(
        token=capability,
        purpose=OtpPurpose.PASSWORD_CHANGE,
        channel=OtpTargetState.Channel.EMAIL,
        target=user.email,
        account=user,
    )

    account = get_user_model().objects.select_for_update().get(pk=user.pk)
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
        action=AuditLog.Action.PASSWORD_CHANGED,
        actor=account,
        resource_type="USER",
        resource_uuid=account.uuid,
        metadata={"sessions_revoked": True},
    )
    return issue_tokens_for_user(account)
