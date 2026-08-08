from django.urls import path

from patients.api import PatientMeView

app_name = "patients"

urlpatterns = [path("me/", PatientMeView.as_view(), name="me")]
