from django.urls import path

from patients.api import PatientAvatarView, PatientMeView

app_name = "patients"

urlpatterns = [
    path("me/", PatientMeView.as_view(), name="me"),
    path("me/avatar/", PatientAvatarView.as_view(), name="avatar"),
]
