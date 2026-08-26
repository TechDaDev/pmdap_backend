from django.urls import path

from guardians.api import (
    GuardianRelationshipDismissView,
    GuardianRelationshipPatientCollectionView,
    GuardianRelationshipPatientDetailView,
)

urlpatterns = [
    path(
        "",
        GuardianRelationshipPatientCollectionView.as_view(),
        name="guardian-relationship-list",
    ),
    path(
        "<uuid:relationship_uuid>/",
        GuardianRelationshipPatientDetailView.as_view(),
        name="guardian-relationship-detail",
    ),
    path(
        "<uuid:relationship_uuid>/dismiss/",
        GuardianRelationshipDismissView.as_view(),
        name="guardian-relationship-dismiss",
    ),
]
