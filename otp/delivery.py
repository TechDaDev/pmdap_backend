from abc import ABC, abstractmethod

from django.conf import settings
from django.core.mail import send_mail


class OtpDeliveryService(ABC):
    @abstractmethod
    def send_email_otp(self, *, target, code, expires_minutes, locale="en"):
        raise NotImplementedError


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
