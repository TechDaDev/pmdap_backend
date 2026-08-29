from django.urls import path

from registration.email_api import (
    RegistrationEmailResendView,
    RegistrationEmailStartView,
    RegistrationEmailStatusView,
    RegistrationEmailVerifyView,
)

app_name = "registration_email"

urlpatterns = [
    path("start/", RegistrationEmailStartView.as_view(), name="email-start"),
    path("resend/", RegistrationEmailResendView.as_view(), name="email-resend"),
    path("verify/", RegistrationEmailVerifyView.as_view(), name="email-verify"),
    path("status/", RegistrationEmailStatusView.as_view(), name="email-status"),
]
