from unittest.mock import patch

import pytest
from django.test import override_settings
from resend.exceptions import ResendError

from otp.delivery import ResendOtpDeliveryService
from otp.exceptions import OtpDeliveryFailed, OtpProviderError, OtpRateLimited
from otp.models import OtpChallenge, OtpPurpose
from otp.services import issue_otp


@override_settings(
    RESEND_API_KEY="re_test_only",
    RESEND_FROM_EMAIL="onboarding@resend.dev",
)
def test_resend_delivery_accepts_provider_message_id():
    with patch("resend.Emails.send", return_value={"id": "email_123"}) as send:
        result = ResendOtpDeliveryService().send_email_otp(
            target="owner@example.com",
            code="123456",
            expires_minutes=10,
        )

    assert result.provider_message_id == "email_123"
    send.assert_called_once_with(
        {
            "from": "onboarding@resend.dev",
            "to": ["owner@example.com"],
            "subject": "PMDAP verification code",
            "text": (
                "PMDAP verification code\n\n123456\n\n"
                "Expires in 10 minutes.\n"
                "Do not share this code."
            ),
        }
    )


@override_settings(
    RESEND_API_KEY="re_test_only",
    RESEND_FROM_EMAIL="onboarding@resend.dev",
)
def test_resend_delivery_rejects_response_without_message_id():
    with (
        patch("resend.Emails.send", return_value={}),
        pytest.raises(OtpProviderError, match="OTP email delivery failed"),
    ):
        ResendOtpDeliveryService().send_email_otp(
            target="owner@example.com",
            code="123456",
            expires_minutes=10,
        )


@override_settings(
    RESEND_API_KEY="re_test_only",
    RESEND_FROM_EMAIL="onboarding@resend.dev",
)
def test_resend_delivery_sanitizes_provider_exception():
    provider_message = "request body contained 123456 for owner@example.com"
    with (
        patch("resend.Emails.send", side_effect=ConnectionError(provider_message)),
        pytest.raises(OtpProviderError) as exc,
    ):
        ResendOtpDeliveryService().send_email_otp(
            target="owner@example.com",
            code="123456",
            expires_minutes=10,
        )

    assert str(exc.value) == "OTP email delivery failed."
    assert exc.value.__cause__ is None
    assert provider_message not in str(exc.value)


@override_settings(RESEND_API_KEY="", RESEND_FROM_EMAIL="onboarding@resend.dev")
def test_resend_delivery_fails_safely_without_api_key():
    with (
        patch("resend.Emails.send") as send,
        pytest.raises(OtpProviderError, match="OTP email delivery unavailable"),
    ):
        ResendOtpDeliveryService().send_email_otp(
            target="owner@example.com",
            code="123456",
            expires_minutes=10,
        )

    send.assert_not_called()


@override_settings(
    RESEND_API_KEY="re_test_only",
    RESEND_FROM_EMAIL="onboarding@resend.dev",
)
def test_resend_delivery_does_not_log_otp_or_recipient(caplog):
    with patch("resend.Emails.send", return_value={"id": "email_123"}):
        ResendOtpDeliveryService().send_email_otp(
            target="owner@example.com",
            code="123456",
            expires_minutes=10,
        )

    log_text = " ".join(record.getMessage() for record in caplog.records)
    assert "re_test_only" not in log_text
    assert "123456" not in log_text
    assert "owner@example.com" not in log_text


@pytest.mark.django_db
@override_settings(
    RESEND_API_KEY="re_test_only",
    RESEND_FROM_EMAIL="onboarding@resend.dev",
)
def test_default_resend_rejection_invalidates_challenge():
    with (
        patch("resend.Emails.send", return_value={}),
        pytest.raises(OtpDeliveryFailed, match="OTP delivery failed"),
    ):
        issue_otp(
            purpose=OtpPurpose.EMAIL_VERIFICATION,
            channel="EMAIL",
            target="owner@example.com",
            source="resend-adapter-test",
        )

    challenge = OtpChallenge.objects.get()
    assert challenge.invalidated_at is not None


@override_settings(
    RESEND_API_KEY="re_test_only",
    RESEND_FROM_EMAIL="onboarding@resend.dev",
)
def test_resend_rate_limit_maps_to_otp_rate_limited():
    """Provider HTTP 429 (rate limit / daily quota) is a retryable throttle."""
    provider = ResendError(
        429,
        "rate_limit_exceeded",
        "Daily sending quota exceeded.",
        "Try again later.",
    )
    with (
        patch("resend.Emails.send", side_effect=provider),
        pytest.raises(OtpRateLimited),
    ):
        ResendOtpDeliveryService().send_email_otp(
            target="owner@example.com",
            code="123456",
            expires_minutes=10,
        )


@override_settings(
    RESEND_API_KEY="re_test_only",
    RESEND_FROM_EMAIL="onboarding@resend.dev",
)
def test_resend_rate_limit_logs_provider_code_without_leaking(caplog):
    provider = ResendError(
        429,
        "rate_limit_exceeded",
        "Daily sending quota exceeded.",
        "Try again later.",
    )
    with (
        patch("resend.Emails.send", side_effect=provider),
        pytest.raises(OtpRateLimited),
    ):
        ResendOtpDeliveryService().send_email_otp(
            target="owner@example.com",
            code="123456",
            expires_minutes=10,
        )

    log_text = " ".join(record.getMessage() for record in caplog.records)
    assert "429" in log_text
    assert "quota" in log_text
    assert "123456" not in log_text
    assert "owner@example.com" not in log_text


@override_settings(
    RESEND_API_KEY="re_test_only",
    RESEND_FROM_EMAIL="onboarding@resend.dev",
)
def test_resend_other_provider_error_stays_delivery_failure():
    provider = ResendError(
        403,
        "invalid_api_key",
        "The API key is invalid.",
        "Check your API key.",
    )
    with (
        patch("resend.Emails.send", side_effect=provider),
        pytest.raises(OtpProviderError, match="OTP email delivery failed") as exc,
    ):
        ResendOtpDeliveryService().send_email_otp(
            target="owner@example.com",
            code="123456",
            expires_minutes=10,
        )

    # Provider errors are never chained (their repr may echo the request body).
    assert exc.value.__cause__ is None


@pytest.mark.django_db
@override_settings(
    RESEND_API_KEY="re_test_only",
    RESEND_FROM_EMAIL="onboarding@resend.dev",
)
def test_issue_otp_propagates_rate_limit_and_invalidates_challenge():
    """A provider 429 must surface as OtpRateLimited (not wrapped), and the
    undelivered challenge must be invalidated like any delivery failure."""
    provider = ResendError(429, "rate_limit_exceeded", "Too many requests.", "")
    with (
        patch("resend.Emails.send", side_effect=provider),
        pytest.raises(OtpRateLimited),
    ):
        issue_otp(
            purpose=OtpPurpose.EMAIL_VERIFICATION,
            channel="EMAIL",
            target="owner@example.com",
            source="resend-adapter-test",
        )

    challenge = OtpChallenge.objects.get()
    assert challenge.invalidated_at is not None
