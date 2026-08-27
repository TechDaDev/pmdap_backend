from django.urls import path

from identities.api import (
    VerificationApproveView,
    VerificationCollectionView,
    VerificationCorrectVerifiedView,
    VerificationDetailView,
    VerificationRejectView,
    VerificationReviewFieldsView,
)

urlpatterns = [
    path("", VerificationCollectionView.as_view(), name="identity-verification-list"),
    path(
        "<uuid:document_uuid>/",
        VerificationDetailView.as_view(),
        name="identity-verification-detail",
    ),
    path(
        "<uuid:document_uuid>/approve/",
        VerificationApproveView.as_view(),
        name="identity-verification-approve",
    ),
    path(
        "<uuid:document_uuid>/reject/",
        VerificationRejectView.as_view(),
        name="identity-verification-reject",
    ),
    path(
        "<uuid:document_uuid>/review-fields/",
        VerificationReviewFieldsView.as_view(),
        name="identity-verification-review-fields",
    ),
    path(
        "<uuid:document_uuid>/correct-verified/",
        VerificationCorrectVerifiedView.as_view(),
        name="identity-verification-correct-verified",
    ),
]
