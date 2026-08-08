from django.urls import path

from accounts.api import LoginView, LogoutView, MeView, RefreshView, RegisterView
from claims.api import ClaimedAccountActivationView

app_name = "accounts"

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", LoginView.as_view(), name="login"),
    path("refresh/", RefreshView.as_view(), name="refresh"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("me/", MeView.as_view(), name="me"),
    path(
        "activate-claimed-account/",
        ClaimedAccountActivationView.as_view(),
        name="activate-claimed-account",
    ),
]
