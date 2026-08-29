import json
import re
from datetime import timedelta

import pytest
from django.core import mail
from django.test import override_settings
from django.utils import timezone

from accounts.models import User
from audit.models import AuditLog
from otp.delivery import DjangoOtpDeliveryService
from otp.exceptions import (
    InvalidOtp,
    OtpCooldown,
    OtpDeliveryFailed,
    OtpRateLimited,
    UnsupportedOtpChannel,
)
from otp.models import (
    OtpAuthorization,
    OtpChallenge,
    OtpPurpose,
    OtpRateLimitBucket,
    OtpTargetState,
)
from otp.services import consume_otp_authorization, issue_otp, verify_otp

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


def issue(
    *, target="owner@example.com", purpose=OtpPurpose.EMAIL_VERIFICATION, **kwargs
):
    delivery = kwargs.pop("delivery_service", CapturingDelivery())
    result = issue_otp(
        purpose=purpose,
        channel="EMAIL",
        target=target,
        delivery_service=delivery,
        **kwargs,
    )
    return result, delivery


def allow_resend(target="owner@example.com", purpose=OtpPurpose.EMAIL_VERIFICATION):
    state = (
        OtpTargetState.objects.filter(purpose=purpose).order_by("-created_at").first()
    )
    state.last_issued_at = timezone.now() - timedelta(seconds=61)
    state.save(update_fields=("last_issued_at", "updated_at"))


def test_issue_generates_six_digits_and_stores_only_hash():
    result, delivery = issue()

    code = delivery.messages[0]["code"]
    challenge = OtpChallenge.objects.get(uuid=result.challenge_uuid)

    assert re.fullmatch(r"\d{6}", code)
    assert challenge.code_hash != code
    assert code not in challenge.code_hash
    assert challenge.expires_at == pytest.approx(
        timezone.now() + timedelta(minutes=10), abs=timedelta(seconds=2)
    )
    assert not hasattr(result, "code")


def test_django_delivery_uses_locmem_and_contains_no_identity_data():
    with override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="PMDAP <pmdap@techda.dev>",
    ):
        issue(delivery_service=DjangoOtpDeliveryService())

    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.subject == "PMDAP verification code"
    assert message.from_email == "PMDAP <pmdap@techda.dev>"
    assert message.to == ["owner@example.com"]
    assert "Expires in 10 minutes." in message.body
    assert "Do not share this code." in message.body


def test_correct_verification_consumes_challenge_and_blocks_replay():
    result, delivery = issue()
    code = delivery.messages[0]["code"]

    authorization = verify_otp(
        purpose=OtpPurpose.EMAIL_VERIFICATION,
        channel="EMAIL",
        target="OWNER@example.com ",
        code=code,
    )

    challenge = OtpChallenge.objects.get(uuid=result.challenge_uuid)
    stored_authorization = OtpAuthorization.objects.get(challenge=challenge)
    assert challenge.consumed_at is not None
    assert authorization.token
    assert authorization.token not in stored_authorization.token_hash
    with pytest.raises(InvalidOtp):
        verify_otp(
            purpose=OtpPurpose.EMAIL_VERIFICATION,
            channel="EMAIL",
            target="owner@example.com",
            code=code,
        )


def test_wrong_codes_lock_challenge_after_five_attempts():
    result, delivery = issue()

    for _ in range(4):
        with pytest.raises(InvalidOtp):
            verify_otp(
                purpose=OtpPurpose.EMAIL_VERIFICATION,
                channel="EMAIL",
                target="owner@example.com",
                code="000000",
            )

    challenge = OtpChallenge.objects.get(uuid=result.challenge_uuid)
    assert challenge.failed_attempts == 4
    assert challenge.locked_at is None

    with pytest.raises(InvalidOtp):
        verify_otp(
            purpose=OtpPurpose.EMAIL_VERIFICATION,
            channel="EMAIL",
            target="owner@example.com",
            code="000000",
        )

    challenge.refresh_from_db()
    assert challenge.failed_attempts == 5
    assert challenge.locked_at is not None
    with pytest.raises(InvalidOtp):
        verify_otp(
            purpose=OtpPurpose.EMAIL_VERIFICATION,
            channel="EMAIL",
            target="owner@example.com",
            code=delivery.messages[0]["code"],
        )


def test_expired_code_is_rejected_without_incrementing_attempts():
    result, delivery = issue()
    OtpChallenge.objects.filter(uuid=result.challenge_uuid).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )

    with pytest.raises(InvalidOtp):
        verify_otp(
            purpose=OtpPurpose.EMAIL_VERIFICATION,
            channel="EMAIL",
            target="owner@example.com",
            code=delivery.messages[0]["code"],
        )

    assert OtpChallenge.objects.get(uuid=result.challenge_uuid).failed_attempts == 0


def test_resend_cooldown_and_old_code_invalidation():
    first_result, first_delivery = issue()
    with pytest.raises(OtpCooldown) as exc:
        issue()
    assert 1 <= exc.value.retry_after_seconds <= 60

    allow_resend()
    second_result, second_delivery = issue()

    first = OtpChallenge.objects.get(uuid=first_result.challenge_uuid)
    assert first.invalidated_at is not None
    assert second_result.challenge_uuid != first_result.challenge_uuid
    with pytest.raises(InvalidOtp):
        verify_otp(
            purpose=OtpPurpose.EMAIL_VERIFICATION,
            channel="EMAIL",
            target="owner@example.com",
            code=first_delivery.messages[0]["code"],
        )

    verify_otp(
        purpose=OtpPurpose.EMAIL_VERIFICATION,
        channel="EMAIL",
        target="owner@example.com",
        code=second_delivery.messages[0]["code"],
    )


def test_purpose_and_target_are_isolated():
    _, delivery = issue()
    code = delivery.messages[0]["code"]

    for purpose, target in (
        (OtpPurpose.PASSWORD_RESET, "owner@example.com"),
        (OtpPurpose.EMAIL_VERIFICATION, "other@example.com"),
    ):
        with pytest.raises(InvalidOtp):
            verify_otp(
                purpose=purpose,
                channel="EMAIL",
                target=target,
                code=code,
            )


def test_authorization_artifact_is_purpose_target_bound_and_one_time():
    _, delivery = issue(purpose=OtpPurpose.PASSWORD_CHANGE)
    authorization = verify_otp(
        purpose=OtpPurpose.PASSWORD_CHANGE,
        channel="EMAIL",
        target="owner@example.com",
        code=delivery.messages[0]["code"],
    )

    with pytest.raises(InvalidOtp):
        consume_otp_authorization(
            token=authorization.token,
            purpose=OtpPurpose.PASSWORD_RESET,
            channel="EMAIL",
            target="owner@example.com",
        )

    challenge = consume_otp_authorization(
        token=authorization.token,
        purpose=OtpPurpose.PASSWORD_CHANGE,
        channel="EMAIL",
        target="owner@example.com",
    )
    assert challenge.purpose == OtpPurpose.PASSWORD_CHANGE
    with pytest.raises(InvalidOtp):
        consume_otp_authorization(
            token=authorization.token,
            purpose=OtpPurpose.PASSWORD_CHANGE,
            channel="EMAIL",
            target="owner@example.com",
        )


def test_expired_authorization_artifact_is_rejected():
    _, delivery = issue(purpose=OtpPurpose.PASSWORD_RESET)
    authorization = verify_otp(
        purpose=OtpPurpose.PASSWORD_RESET,
        channel="EMAIL",
        target="owner@example.com",
        code=delivery.messages[0]["code"],
    )
    OtpAuthorization.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

    with pytest.raises(InvalidOtp):
        consume_otp_authorization(
            token=authorization.token,
            purpose=OtpPurpose.PASSWORD_RESET,
            channel="EMAIL",
            target="owner@example.com",
        )


def test_account_binding_blocks_other_account_without_consuming_code():
    owner = User.objects.create_user(email="owner@example.com", password="SafePass123!")
    other = User.objects.create_user(email="other@example.com", password="SafePass123!")
    result, delivery = issue(account=owner)

    with pytest.raises(InvalidOtp):
        verify_otp(
            purpose=OtpPurpose.EMAIL_VERIFICATION,
            channel="EMAIL",
            target="owner@example.com",
            code=delivery.messages[0]["code"],
            account=other,
        )

    assert OtpChallenge.objects.get(uuid=result.challenge_uuid).consumed_at is None
    verify_otp(
        purpose=OtpPurpose.EMAIL_VERIFICATION,
        channel="EMAIL",
        target="owner@example.com",
        code=delivery.messages[0]["code"],
        account=owner,
    )


def test_delivery_failure_invalidates_challenge():
    class FailingDelivery:
        def send_email_otp(self, **kwargs):
            raise ConnectionError("SMTP unavailable")

    with pytest.raises(OtpDeliveryFailed) as exc:
        issue(delivery_service=FailingDelivery())

    challenge = OtpChallenge.objects.get()
    assert challenge.invalidated_at is not None
    assert "SMTP unavailable" not in str(exc.value)


def test_sms_and_unknown_purpose_are_not_issuable():
    with pytest.raises(UnsupportedOtpChannel):
        issue_otp(
            purpose=OtpPurpose.PHONE_VERIFICATION,
            channel="SMS",
            target="+9647000000000",
            delivery_service=CapturingDelivery(),
        )
    with pytest.raises(ValueError):
        issue_otp(
            purpose="ARBITRARY",
            channel="EMAIL",
            target="owner@example.com",
            delivery_service=CapturingDelivery(),
        )


@override_settings(
    OTP_ISSUE_LIMIT_TARGET=2,
    OTP_ISSUE_LIMIT_ACCOUNT=2,
    OTP_ISSUE_LIMIT_SOURCE=2,
)
def test_postgresql_backed_target_account_and_source_throttling():
    user = User.objects.create_user(
        email="account@example.com", password="SafePass123!"
    )

    issue(target="one@example.com", account=user, source="192.0.2.1")
    issue(target="two@example.com", account=user, source="192.0.2.1")

    with pytest.raises(OtpRateLimited):
        issue(target="three@example.com", account=user, source="192.0.2.1")

    issue(target="target@example.com", source="198.51.100.1")
    allow_resend(target="target@example.com")
    issue(target="target@example.com", source="198.51.100.2")
    allow_resend(target="target@example.com")
    with pytest.raises(OtpRateLimited):
        issue(target="target@example.com", source="198.51.100.3")


@override_settings(
    OTP_ISSUE_LIMIT_TARGET=10,
    OTP_ISSUE_LIMIT_ACCOUNT=10,
    OTP_ISSUE_LIMIT_SOURCE=1,
)
def test_source_throttle_is_independent_of_target():
    issue(target="one@example.com", source="203.0.113.4")
    with pytest.raises(OtpRateLimited):
        issue(target="two@example.com", source="203.0.113.4")


@override_settings(
    OTP_ISSUE_LIMIT_TARGET=1,
    OTP_ISSUE_LIMIT_ACCOUNT=10,
    OTP_ISSUE_LIMIT_SOURCE=10,
)
def test_expired_rate_window_resets():
    issue(source="203.0.113.8")
    allow_resend()
    OtpRateLimitBucket.objects.update(
        window_started_at=timezone.now() - timedelta(hours=2)
    )
    issue(source="203.0.113.8")


def test_audit_and_logs_never_contain_code_or_target(caplog):
    result, delivery = issue()
    code = delivery.messages[0]["code"]
    with pytest.raises(InvalidOtp):
        verify_otp(
            purpose=OtpPurpose.EMAIL_VERIFICATION,
            channel="EMAIL",
            target="owner@example.com",
            code="000000",
        )

    audit_text = json.dumps(
        list(
            AuditLog.objects.filter(resource_uuid=result.challenge_uuid).values(
                "action", "previous_values", "new_values", "metadata"
            )
        ),
        default=str,
    )
    log_text = " ".join(record.getMessage() for record in caplog.records)
    assert code not in audit_text
    assert code not in log_text
    assert "owner@example.com" not in audit_text
    assert "owner@example.com" not in log_text


def test_declared_purposes_and_channels_cover_future_design():
    assert set(OtpPurpose.values) == {
        "EMAIL_VERIFICATION",
        "PASSWORD_RESET",
        "PASSWORD_CHANGE",
        "EMAIL_CHANGE",
        "PHONE_VERIFICATION",
    }
    assert set(OtpChallenge.Channel.values) == {"EMAIL", "SMS"}
