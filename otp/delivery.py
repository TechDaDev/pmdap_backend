from abc import ABC, abstractmethod
from dataclasses import dataclass

import resend
from django.conf import settings
from django.core.mail import send_mail

from otp.exceptions import OtpProviderError


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
        except Exception:
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
