"""M32B authenticated password change tests — SYNTHETIC data only.

Covers current-password proof, OTP issuance/verification, capability binding,
replay/expiry/cross-user denial, password policy, session revocation, and
no-secret audit logging. OTP codes are read from the locmem email outbox and
never appear in assertions or logs.
"""

import re
from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User
from audit.models import AuditLog
from otp.models import OtpAuthorization, OtpChallenge, OtpPurpose, OtpTargetState
from otp.services import issue_otp
from tests.factories import UserFactory

REQUEST = "/api/v1/auth/password-change/request/"
VERIFY = "/api/v1/auth/password-change/verify/"
CONFIRM = "/api/v1/auth/password-change/confirm/"
LOGIN = "/api/v1/auth/login/"
REFRESH = "/api/v1/auth/refresh/"
ME = "/api/v1/auth/me/"
OLD_PASSWORD = "Correct-Horse-Battery-42!"
NEW_PASSWORD = "Fresh-Correct-Horse-84!"

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clear_throttle_cache():
    from django.core.cache import cache

    cache.clear()


@pytest.fixture(autouse=True)
def clear_otp_buckets():
    """Reset the PostgreSQL OTP rate-limit buckets between tests.

    The source bucket is keyed by the shared test client IP, so without this
    the suite exhausts OTP_ISSUE_LIMIT_SOURCE and starts 429ing.
    """
    from otp.models import OtpRateLimitBucket

    OtpRateLimitBucket.objects.all().delete()


@pytest.fixture
def user():
    return UserFactory(
        email="owner@example.com",
        password=OLD_PASSWORD,
        status=User.Status.ACTIVE,
        is_active=True,
        email_verified=True,
        email_verified_at=timezone.now(),
    )


@pytest.fixture
def other():
    return UserFactory(
        email="other@example.com",
        password="Other-Pass-123!",
        status=User.Status.ACTIVE,
        is_active=True,
        email_verified=True,
        email_verified_at=timezone.now(),
    )


def _auth(user):
    return f"Bearer {RefreshToken.for_user(user).access_token}"


def _authed(api_client, user):
    api_client.credentials(HTTP_AUTHORIZATION=_auth(user))


def otp_code():
    body = mail.outbox[-1].body
    match = re.search(r"\n\n(\d{6})\n\n", body)
    assert match, "OTP code not found in delivered email body"
    return match.group(1)


def assert_error(response, status_code, code=None):
    assert response.status_code == status_code
    assert set(response.json()) == {"error"}
    error = response.json()["error"]
    assert set(error) == {"code", "message", "details"}
    if code:
        assert error["code"] == code


def request_change(api_client, password=OLD_PASSWORD):
    return api_client.post(REQUEST, {"current_password": password}, format="json")


def verify_change(api_client, code=None):
    code = code or otp_code()
    return api_client.post(VERIFY, {"code": code}, format="json")


def confirm_change(api_client, capability, password=NEW_PASSWORD):
    return api_client.post(
        CONFIRM, {"capability": capability, "new_password": password}, format="json"
    )


def start_verified_change(api_client):
    assert request_change(api_client).status_code == 200
    response = verify_change(api_client)
    assert response.status_code == 200, response.content
    return response.json()["data"]["capability"]


def login(api_client, password):
    return api_client.post(
        LOGIN,
        {"email": "owner@example.com", "password": password},
        format="json",
    )


# --------------------------------------------------------------------------- #
# AUTHENTICATION GATE
# --------------------------------------------------------------------------- #


def test_unauthenticated_denied(api_client):
    for url, payload in (
        (REQUEST, {"current_password": OLD_PASSWORD}),
        (VERIFY, {"code": "123456"}),
        (CONFIRM, {"capability": "x", "new_password": NEW_PASSWORD}),
    ):
        response = api_client.post(url, payload, format="json")
        assert response.status_code == 401


# --------------------------------------------------------------------------- #
# REQUEST
# --------------------------------------------------------------------------- #


def test_request_correct_current_password_sends_otp(api_client, user):
    _authed(api_client, user)
    response = request_change(api_client)

    assert response.status_code == 200
    assert response.json()["data"]["resend_after_seconds"] == 60
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["owner@example.com"]
    code = otp_code()
    challenge = OtpChallenge.objects.get(
        state__purpose=OtpPurpose.PASSWORD_CHANGE,
        account=user,
        consumed_at__isnull=True,
        invalidated_at__isnull=True,
    )
    assert challenge.code_hash != code
    assert code not in challenge.code_hash


def test_request_wrong_current_password_denied(api_client, user):
    _authed(api_client, user)
    response = request_change(api_client, password="Wrong-Pass-999!")

    assert_error(response, 400, "password_change_wrong_current_password")
    assert len(mail.outbox) == 0
    assert not OtpChallenge.objects.filter(account=user).exists()


def test_request_unverified_email_denied(api_client, user):
    user.email_verified = False
    user.email_verified_at = None
    user.save(update_fields=("email_verified", "email_verified_at"))
    _authed(api_client, user)

    response = request_change(api_client)
    assert_error(response, 400, "password_change_email_unverified")
    assert len(mail.outbox) == 0


def test_request_never_accepts_client_target(api_client, user):
    _authed(api_client, user)
    response = api_client.post(
        REQUEST,
        {
            "current_password": OLD_PASSWORD,
            "email": "attacker@example.com",
            "target": "attacker@example.com",
        },
        format="json",
    )
    # Unknown fields are rejected by RejectUnknownFieldsMixin.
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# VERIFY
# --------------------------------------------------------------------------- #


def test_verify_issues_short_lived_capability(api_client, user):
    _authed(api_client, user)
    assert request_change(api_client).status_code == 200
    response = verify_change(api_client)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["capability"]
    assert data["expires_at"]
    challenge = OtpChallenge.objects.get(account=user)
    assert challenge.consumed_at is not None


def test_otp_replay_denied(api_client, user):
    _authed(api_client, user)
    assert request_change(api_client).status_code == 200
    code = otp_code()
    assert verify_change(api_client, code).status_code == 200
    assert_error(verify_change(api_client, code), 400, "password_change_otp_invalid")


def test_wrong_otp_locks_challenge(api_client, user):
    _authed(api_client, user)
    assert request_change(api_client).status_code == 200
    for _ in range(5):
        assert verify_change(api_client, "000000").status_code == 400
    # Even the correct code is now locked.
    assert_error(verify_change(api_client), 400, "password_change_otp_invalid")


def test_expired_otp_denied(api_client, user):
    _authed(api_client, user)
    assert request_change(api_client).status_code == 200
    challenge = OtpChallenge.objects.get(account=user)
    challenge.expires_at = timezone.now() - timedelta(seconds=1)
    challenge.save(update_fields=("expires_at", "updated_at"))
    assert_error(verify_change(api_client), 400, "password_change_otp_invalid")


def test_purpose_isolation_reset_code_rejected(api_client, user):
    """A PASSWORD_RESET OTP cannot authorize a PASSWORD_CHANGE."""
    from otp.delivery import get_otp_delivery_service

    issue_otp(
        purpose=OtpPurpose.PASSWORD_RESET,
        channel=OtpTargetState.Channel.EMAIL,
        target=user.email,
        account=user,
        delivery_service=get_otp_delivery_service(),
    )
    reset_code = otp_code()
    _authed(api_client, user)
    assert_error(
        verify_change(api_client, reset_code), 400, "password_change_otp_invalid"
    )


def test_cross_user_cannot_use_otp(api_client, user, other):
    """An OTP issued for user A cannot be verified by user B."""
    _authed(api_client, user)
    assert request_change(api_client).status_code == 200
    code = otp_code()

    _authed(api_client, other)
    assert_error(verify_change(api_client, code), 400, "password_change_otp_invalid")


# --------------------------------------------------------------------------- #
# CONFIRM
# --------------------------------------------------------------------------- #


def test_confirm_changes_password_and_returns_fresh_tokens(api_client, user):
    _authed(api_client, user)
    capability = start_verified_change(api_client)
    response = confirm_change(api_client, capability)

    assert response.status_code == 200, response.content
    data = response.json()["data"]
    assert data["access"] and data["refresh"]
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
    assert not user.check_password(OLD_PASSWORD)


def test_capability_replay_denied(api_client, user):
    _authed(api_client, user)
    capability = start_verified_change(api_client)
    first = confirm_change(api_client, capability)
    assert first.status_code == 200
    # The change rotates the acting session, so use the fresh token issued by
    # the first confirm for the replay attempt.
    fresh_access = first.json()["data"]["access"]
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {fresh_access}")
    assert_error(
        confirm_change(api_client, capability),
        400,
        "password_change_capability_invalid",
    )


def test_capability_expiry_denied(api_client, user):
    _authed(api_client, user)
    capability = start_verified_change(api_client)
    authorization = OtpAuthorization.objects.get(
        challenge__account=user,
        challenge__state__purpose=OtpPurpose.PASSWORD_CHANGE,
    )
    authorization.expires_at = timezone.now() - timedelta(seconds=1)
    authorization.save(update_fields=("expires_at", "updated_at"))
    assert_error(
        confirm_change(api_client, capability),
        400,
        "password_change_capability_invalid",
    )


def test_capability_cross_user_denied(api_client, user, other):
    _authed(api_client, user)
    capability = start_verified_change(api_client)

    _authed(api_client, other)
    assert_error(
        confirm_change(api_client, capability),
        400,
        "password_change_capability_invalid",
    )
    other.refresh_from_db()
    assert other.check_password("Other-Pass-123!")


def test_weak_new_password_rejected_without_consuming_capability(api_client, user):
    _authed(api_client, user)
    capability = start_verified_change(api_client)
    response = confirm_change(api_client, capability, password="short")

    assert response.status_code == 400
    assert "new_password" in response.json()["error"]["details"]
    user.refresh_from_db()
    assert user.check_password(OLD_PASSWORD)
    # Capability NOT consumed: a valid password can retry with it.
    assert confirm_change(api_client, capability).status_code == 200


# --------------------------------------------------------------------------- #
# SESSION POLICY
# --------------------------------------------------------------------------- #


def test_old_password_denied_new_password_works(api_client, user):
    _authed(api_client, user)
    capability = start_verified_change(api_client)
    assert confirm_change(api_client, capability).status_code == 200

    assert login(api_client, OLD_PASSWORD).status_code == 401
    assert login(api_client, NEW_PASSWORD).status_code == 200


def test_other_pre_change_sessions_revoked_current_preserved(api_client, user):
    # Session A: pre-change login (would be a separate device).
    pre = login(api_client, OLD_PASSWORD)
    assert pre.status_code == 200
    pre_access = pre.json()["data"]["access"]
    pre_refresh = pre.json()["data"]["refresh"]

    # Session B (acting device) changes the password and gets fresh tokens.
    _authed(api_client, user)
    capability = start_verified_change(api_client)
    confirmed = confirm_change(api_client, capability)
    assert confirmed.status_code == 200
    fresh_access = confirmed.json()["data"]["access"]
    fresh_refresh = confirmed.json()["data"]["refresh"]

    # Pre-change access token revoked (session_version bump).
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {pre_access}")
    assert api_client.get(ME).status_code == 401
    # Pre-change refresh token blacklisted. RefreshView requires a valid
    # access-token header, so authenticate with the fresh current token.
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {fresh_access}")
    assert (
        api_client.post(REFRESH, {"refresh": pre_refresh}, format="json").status_code
        == 401
    )
    # Fresh (current-device) tokens work.
    assert api_client.get(ME).status_code == 200
    assert (
        api_client.post(REFRESH, {"refresh": fresh_refresh}, format="json").status_code
        == 200
    )


# --------------------------------------------------------------------------- #
# AUDIT / NO SECRET LOGGING
# --------------------------------------------------------------------------- #


def test_audit_records_purpose_only_no_secrets(api_client, user):
    _authed(api_client, user)
    capability = start_verified_change(api_client)
    assert confirm_change(api_client, capability).status_code == 200

    requested = AuditLog.objects.get(
        action=AuditLog.Action.PASSWORD_CHANGE_OTP_REQUESTED
    )
    verified = AuditLog.objects.get(action=AuditLog.Action.PASSWORD_CHANGE_OTP_VERIFIED)
    changed = AuditLog.objects.get(action=AuditLog.Action.PASSWORD_CHANGED)

    assert requested.metadata == {"purpose": "PASSWORD_CHANGE"}
    assert verified.metadata == {"purpose": "PASSWORD_CHANGE"}
    assert changed.metadata == {"sessions_revoked": True}
    # Serialized audit payloads must not contain the password or capability.
    for log in (requested, verified, changed):
        dump = repr(
            {
                "metadata": log.metadata,
                "new_values": log.new_values,
                "previous_values": log.previous_values,
            }
        )
        assert NEW_PASSWORD not in dump
        assert OLD_PASSWORD not in dump
        assert capability not in dump
