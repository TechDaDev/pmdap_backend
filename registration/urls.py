from django.urls import path

from registration.api import (
    RegistrationIdentityExtractView,
    RegistrationIdentityStatusView,
)

urlpatterns = [
    path(
        "extract/",
        RegistrationIdentityExtractView.as_view(),
        name="register-identity-extract",
    ),
    path(
        "extract/<uuid:job_id>/",
        RegistrationIdentityStatusView.as_view(),
        name="register-identity-extract-status",
    ),
]
