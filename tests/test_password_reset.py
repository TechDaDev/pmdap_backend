from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone

from accounts.models import User
from audit.models import AuditLog
from otp.models import OtpAuthorization, OtpChallenge, OtpPurpose, OtpTargetState
from otp.services import issue_otp
from tests.factories import UserFactory

REQUEST = "/api/v1/auth/password-reset/request/"
VERIFY = "/api/v1/auth/password-reset/verify/"
CONFIRM = "/api/v1/auth/password-reset/confirm/"
LOGIN = "/api/v1/auth/login/"
REFRESH = "/api/v1/auth/refresh/"
ME = "/api/v1/auth/me/"
OLD_PASSWORD = "Correct-Horse-Battery-42!"
NEW_PASSWORD = "Fresh-Correct-Horse-84!"
GENERIC_MESSAGE = "If an eligible account exists, a verification code has been sent."

pytestmark = pytest.mark.django_db


class CapturingDelivery:
    def __init__(self):
        self.messages = []

    def send_email_otp(self, *, target, code, expires_minutes, locale="en"):
        self.messages.append(
            {
                "target": target,
                "code": code,
                "expires_minutes": expires_minutes,
                "locale": locale,
            }
        )


@pytest.fixture(autouse=True)
def clear_throttle_cache():
    cache.clear()


@pytest.fixture
def delivery(monkeypatch):
    sender = CapturingDelivery()
    monkeypatch.setattr("otp.services.ResendOtpDeliveryService", lambda: sender)
    return sender


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


def assert_error(response, status_code, code=None):
    assert response.status_code == status_code
    assert set(response.json()) == {"error"}
    error = response.json()["error"]
    assert set(error) == {"code", "message", "details"}
    if code:
        assert error["code"] == code


def request_reset(api_client, email="owner@example.com"):
    return api_client.post(REQUEST, {"email": email}, format="json")


def allow_resend(email="owner@example.com"):
    state = OtpTargetState.objects.get(
        purpose=OtpPurpose.PASSWORD_RESET,
        channel=OtpTargetState.Channel.EMAIL,
        target_hash__isnull=False,
    )
    state.last_issued_at = timezone.now() - timedelta(seconds=61)
    state.save(update_fields=("last_issued_at", "updated_at"))


def verify_reset(api_client, delivery, email="owner@example.com", code=None):
    code = code or delivery.messages[-1]["code"]
    return api_client.post(
        VERIFY,
        {"email": email, "code": code},
        format="json",
    )


def start_verified_reset(api_client, delivery, user):
    response = request_reset(api_client, user.email)
    assert response.status_code == 200
    response = verify_reset(api_client, delivery, user.email)
    assert response.status_code == 200
    return response.json()["data"]["reset_token"]


def confirm_reset(api_client, token, password=NEW_PASSWORD):
    return api_client.post(
        CONFIRM,
        {"reset_token": token, "new_password": password},
        format="json",
    )


def test_request_known_account_sends_password_reset_otp(api_client, delivery, user):
    response = request_reset(api_client, " OWNER@EXAMPLE.COM ")

    assert response.status_code == 200
    assert response.json() == {
        "data": {
            "message": GENERIC_MESSAGE,
            "resend_after_seconds": 60,
        }
    }
    assert delivery.messages[0]["target"] == user.email
    challenge = OtpChallenge.objects.get()
    assert challenge.purpose == OtpPurpose.PASSWORD_RESET
    assert challenge.account == user
    assert delivery.messages[0]["code"] not in challenge.code_hash


@pytest.mark.parametrize(
    "email,user_fields",
    [
        ("missing@example.com", None),
        ("unverified@example.com", {"email_verified": False}),
        ("disabled@example.com", {"status": User.Status.DISABLED}),
        ("inactive@example.com", {"is_active": False}),
    ],
)
def test_request_non_actionable_account_is_indistinguishable(
    api_client, delivery, email, user_fields
):
    if user_fields is not None:
        fields = {
            "email": email,
            "password": OLD_PASSWORD,
            "status": User.Status.ACTIVE,
            "is_active": True,
            "email_verified": True,
        }
        fields.update(user_fields)
        UserFactory(**fields)

    response = request_reset(api_client, email)

    assert response.status_code == 200
    assert response.json() == {
        "data": {
            "message": GENERIC_MESSAGE,
            "resend_after_seconds": 60,
        }
    }
    assert delivery.messages == []


def test_request_resend_cooldown_applies_to_unknown_target(api_client, delivery):
    assert request_reset(api_client, "missing@example.com").status_code == 200

    response = request_reset(api_client, "missing@example.com")

    assert_error(response, 429, "throttled")
    assert delivery.messages == []


def test_resend_invalidates_old_otp(api_client, delivery, user):
    assert request_reset(api_client).status_code == 200
    old_code = delivery.messages[-1]["code"]
    allow_resend()
    assert request_reset(api_client).status_code == 200

    rejected = verify_reset(api_client, delivery, code=old_code)
    accepted = verify_reset(api_client, delivery)

    assert_error(rejected, 400, "password_reset_otp_invalid")
    assert accepted.status_code == 200


def test_wrong_otp_locks_challenge(api_client, delivery, user):
    assert request_reset(api_client).status_code == 200
    for _ in range(5):
        response = verify_reset(api_client, delivery, code="000000")
        assert_error(response, 400, "password_reset_otp_invalid")

    challenge = OtpChallenge.objects.get()
    assert challenge.failed_attempts == 5
    assert challenge.locked_at is not None
    assert_error(verify_reset(api_client, delivery), 400, "password_reset_otp_invalid")


def test_expired_otp_is_rejected(api_client, delivery, user):
    assert request_reset(api_client).status_code == 200
    OtpChallenge.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

    response = verify_reset(api_client, delivery)

    assert_error(response, 400, "password_reset_otp_invalid")


def test_purpose_isolation_rejects_non_reset_otp(api_client, delivery, user):
    issue_otp(
        purpose=OtpPurpose.EMAIL_VERIFICATION,
        channel=OtpTargetState.Channel.EMAIL,
        target=user.email,
        account=user,
    )

    response = verify_reset(api_client, delivery)

    assert_error(response, 400, "password_reset_otp_invalid")


def test_verify_consumes_otp_and_issues_hashed_reset_capability(
    api_client, delivery, user
):
    assert request_reset(api_client).status_code == 200

    response = verify_reset(api_client, delivery)

    assert response.status_code == 200
    assert set(response.json()["data"]) == {"reset_token", "expires_at"}
    raw_token = response.json()["data"]["reset_token"]
    authorization = OtpAuthorization.objects.get()
    assert raw_token
    assert raw_token not in authorization.token_hash
    assert authorization.expires_at <= timezone.now() + timedelta(minutes=5, seconds=2)
    assert_error(verify_reset(api_client, delivery), 400, "password_reset_otp_invalid")


def test_expired_reset_capability_is_rejected(api_client, delivery, user):
    token = start_verified_reset(api_client, delivery, user)
    OtpAuthorization.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

    response = confirm_reset(api_client, token)

    assert_error(response, 400, "password_reset_capability_invalid")
    user.refresh_from_db()
    assert user.check_password(OLD_PASSWORD)


def test_reset_capability_is_single_use_and_changes_only_bound_account(
    api_client, delivery, user
):
    other = UserFactory(
        email="other@example.com",
        password=OLD_PASSWORD,
        status=User.Status.ACTIVE,
        is_active=True,
        email_verified=True,
    )
    token = start_verified_reset(api_client, delivery, user)

    first = confirm_reset(api_client, token)
    second = confirm_reset(api_client, token, "Another-Fresh-Password-96!")

    assert first.status_code == 200
    assert first.json() == {
        "data": {"message": "Password reset completed. Sign in again."}
    }
    assert_error(second, 400, "password_reset_capability_invalid")
    user.refresh_from_db()
    other.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
    assert other.check_password(OLD_PASSWORD)


def test_new_recovery_flow_invalidates_superseded_capability(
    api_client, delivery, user
):
    first_token = start_verified_reset(api_client, delivery, user)
    allow_resend()
    assert request_reset(api_client).status_code == 200

    response = confirm_reset(api_client, first_token)

    assert_error(response, 400, "password_reset_capability_invalid")


@pytest.mark.parametrize("weak_password", ["password", "12345678", "owner@example.com"])
def test_confirm_reuses_django_password_policy(
    api_client, delivery, user, weak_password
):
    token = start_verified_reset(api_client, delivery, user)

    response = confirm_reset(api_client, token, weak_password)

    assert_error(response, 400, "validation_error")
    assert "new_password" in response.json()["error"]["details"]
    assert OtpAuthorization.objects.get().consumed_at is None


def test_success_denies_old_password_accepts_new_and_revokes_old_tokens(
    api_client, delivery, user
):
    login = api_client.post(
        LOGIN,
        {"email": user.email, "password": OLD_PASSWORD},
        format="json",
    )
    assert login.status_code == 200
    old_access = login.json()["data"]["access"]
    old_refresh = login.json()["data"]["refresh"]
    token = start_verified_reset(api_client, delivery, user)

    assert confirm_reset(api_client, token).status_code == 200

    assert (
        api_client.post(
            LOGIN, {"email": user.email, "password": OLD_PASSWORD}, format="json"
        ).status_code
        == 401
    )
    assert (
        api_client.post(
            LOGIN, {"email": user.email, "password": NEW_PASSWORD}, format="json"
        ).status_code
        == 200
    )
    assert (
        api_client.get(ME, HTTP_AUTHORIZATION=f"Bearer {old_access}").status_code == 401
    )
    assert (
        api_client.post(REFRESH, {"refresh": old_refresh}, format="json").status_code
        == 401
    )


def test_password_reset_audit_and_logs_contain_no_secrets(
    api_client, delivery, user, caplog
):
    assert request_reset(api_client).status_code == 200
    code = delivery.messages[-1]["code"]
    verified = verify_reset(api_client, delivery, code=code)
    token = verified.json()["data"]["reset_token"]
    assert confirm_reset(api_client, token).status_code == 200

    serialized_audit = " ".join(
        f"{entry.action} {entry.metadata} {entry.previous_values} {entry.new_values}"
        for entry in AuditLog.objects.all()
    )
    logs = caplog.text
    for secret in (code, token, OLD_PASSWORD, NEW_PASSWORD, user.email):
        assert secret not in serialized_audit
        assert secret not in logs
    assert {
        AuditLog.Action.PASSWORD_RESET_OTP_REQUESTED,
        AuditLog.Action.PASSWORD_RESET_OTP_VERIFIED,
        AuditLog.Action.PASSWORD_RESET_COMPLETED,
    } <= set(AuditLog.objects.values_list("action", flat=True))
