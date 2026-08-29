from django.urls import path

from accounts.api import LoginView, LogoutView, MeView, RefreshView, RegisterView
from accounts.password_change_api import (
    PasswordChangeConfirmView,
    PasswordChangeRequestView,
    PasswordChangeVerifyView,
)
from accounts.password_reset_api import (
    PasswordResetConfirmView,
    PasswordResetRequestView,
    PasswordResetVerifyView,
)
from claims.api import ClaimedAccountActivationView

app_name = "accounts"

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", LoginView.as_view(), name="login"),
    path("refresh/", RefreshView.as_view(), name="refresh"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("me/", MeView.as_view(), name="me"),
    path(
        "password-reset/request/",
        PasswordResetRequestView.as_view(),
        name="password-reset-request",
    ),
    path(
        "password-reset/verify/",
        PasswordResetVerifyView.as_view(),
        name="password-reset-verify",
    ),
    path(
        "password-reset/confirm/",
        PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path(
        "password-change/request/",
        PasswordChangeRequestView.as_view(),
        name="password-change-request",
    ),
    path(
        "password-change/verify/",
        PasswordChangeVerifyView.as_view(),
        name="password-change-verify",
    ),
    path(
        "password-change/confirm/",
        PasswordChangeConfirmView.as_view(),
        name="password-change-confirm",
    ),
    path(
        "activate-claimed-account/",
        ClaimedAccountActivationView.as_view(),
        name="activate-claimed-account",
    ),
]
