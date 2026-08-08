from django.urls import path

from claims.api import (
    AccountClaimApproveView,
    AccountClaimEvidenceImageView,
    AccountClaimMoreInformationView,
    AccountClaimRejectView,
    AccountClaimVerificationCollectionView,
    AccountClaimVerificationDetailView,
)

urlpatterns = [
    path(
        "", AccountClaimVerificationCollectionView.as_view(), name="account-claim-list"
    ),
    path(
        "<uuid:claim_uuid>/",
        AccountClaimVerificationDetailView.as_view(),
        name="account-claim-detail",
    ),
    path(
        "<uuid:claim_uuid>/approve/",
        AccountClaimApproveView.as_view(),
        name="account-claim-approve",
    ),
    path(
        "<uuid:claim_uuid>/reject/",
        AccountClaimRejectView.as_view(),
        name="account-claim-reject",
    ),
    path(
        "<uuid:claim_uuid>/request-more-information/",
        AccountClaimMoreInformationView.as_view(),
        name="account-claim-more-information",
    ),
    path(
        "<uuid:claim_uuid>/evidence/<uuid:evidence_uuid>/images/<str:side>/",
        AccountClaimEvidenceImageView.as_view(),
        name="account-claim-evidence-image",
    ),
]
