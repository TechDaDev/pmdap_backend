from django.urls import path

from claims.api import AccountClaimSubmissionView

urlpatterns = [
    path("", AccountClaimSubmissionView.as_view(), name="account-claim-submit")
]
