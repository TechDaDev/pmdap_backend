from django.urls import path

from guardians.api import (
    GuardianEvidenceFileView,
    GuardianVerificationApproveView,
    GuardianVerificationCollectionView,
    GuardianVerificationDetailView,
    GuardianVerificationRejectView,
    GuardianVerificationRevokeView,
)

urlpatterns = [
    path(
        "",
        GuardianVerificationCollectionView.as_view(),
        name="guardian-verification-list",
    ),
    path(
        "<uuid:relationship_uuid>/",
        GuardianVerificationDetailView.as_view(),
        name="guardian-verification-detail",
    ),
    path(
        "<uuid:relationship_uuid>/approve/",
        GuardianVerificationApproveView.as_view(),
        name="guardian-verification-approve",
    ),
    path(
        "<uuid:relationship_uuid>/reject/",
        GuardianVerificationRejectView.as_view(),
        name="guardian-verification-reject",
    ),
    path(
        "<uuid:relationship_uuid>/revoke/",
        GuardianVerificationRevokeView.as_view(),
        name="guardian-verification-revoke",
    ),
    path(
        "<uuid:relationship_uuid>/evidence/<uuid:evidence_uuid>/file/",
        GuardianEvidenceFileView.as_view(),
        name="guardian-evidence-file",
    ),
]
