import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

import resend
from django.conf import settings
from django.core.mail import send_mail
from resend.exceptions import ResendError

from otp.exceptions import OtpProviderError, OtpRateLimited

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OtpDeliveryResult:
    provider_message_id: str


def get_otp_delivery_service():
    """Resend when configured, otherwise Django's configured email backend.

    Central resolver so every OTP purpose (verification, reset, change) uses
    the same production/test delivery behavior.
    """
    if getattr(settings, "RESEND_API_KEY", ""):
        return ResendOtpDeliveryService()
    return DjangoOtpDeliveryService()


class OtpDeliveryService(ABC):
    @abstractmethod
    def send_email_otp(self, *, target, code, expires_minutes, locale="en"):
        raise NotImplementedError


class ResendOtpDeliveryService(OtpDeliveryService):
    def send_email_otp(self, *, target, code, expires_minutes, locale="en"):
        api_key = settings.RESEND_API_KEY
        from_email = settings.RESEND_FROM_EMAIL
        if not api_key or not from_email:
            raise OtpProviderError("OTP email delivery unavailable.")

        resend.api_key = api_key
        params: resend.Emails.SendParams = {
            "from": from_email,
            "to": [target],
            "subject": "PMDAP verification code",
            "text": (
                f"PMDAP verification code\n\n{code}\n\n"
                f"Expires in {expires_minutes} minutes.\n"
                "Do not share this code."
            ),
        }
        try:
            response = resend.Emails.send(params)
        except ResendError as exc:
            # Provider rejected the send. Resend API error messages never echo
            # the OTP code or the recipient address, so logging the provider's
            # code/type/message is safe and gives ops a real root cause. A
            # rate-limit or daily-quota rejection (HTTP 429) is retryable and
            # maps to the OTP rate-limited signal so the API returns a 429
            # with a retry hint instead of a hard delivery failure. The raw
            # exception is deliberately NOT chained (its repr may echo the
            # request body).
            logger.warning(
                "OTP email delivery rejected by provider (code=%s type=%s): %s",
                exc.code,
                exc.error_type,
                exc.message,
            )
            if str(exc.code) == "429":
                raise OtpRateLimited() from None
            raise OtpProviderError("OTP email delivery failed.") from None
        except Exception as exc:
            # Transport-level failure. Only the exception type is logged (a
            # message may echo the request body with the OTP/recipient).
            logger.warning("OTP email delivery transport error: %s", type(exc).__name__)
            raise OtpProviderError("OTP email delivery failed.") from None

        message_id = (
            response.get("id")
            if isinstance(response, dict)
            else getattr(response, "id", None)
        )
        if not message_id:
            raise OtpProviderError("OTP email delivery failed.")
        return OtpDeliveryResult(provider_message_id=str(message_id))


class DjangoOtpDeliveryService(OtpDeliveryService):
    def send_email_otp(self, *, target, code, expires_minutes, locale="en"):
        subject = "PMDAP verification code"
        body = (
            f"PMDAP verification code\n\n{code}\n\n"
            f"Expires in {expires_minutes} minutes.\n"
            "Do not share this code."
        )
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[target],
            fail_silently=False,
        )
