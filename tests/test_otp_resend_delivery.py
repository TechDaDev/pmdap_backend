from unittest.mock import patch

import pytest
from django.test import override_settings

from otp.delivery import ResendOtpDeliveryService
from otp.exceptions import OtpDeliveryFailed, OtpProviderError
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
