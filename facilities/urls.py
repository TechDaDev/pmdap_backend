from django.urls import path

from facilities.api import (
    HealthcareFacilityCollectionView,
    HealthcareFacilityDetailView,
)

urlpatterns = [
    path("", HealthcareFacilityCollectionView.as_view(), name="facility-list"),
    path(
        "<uuid:facility_uuid>/",
        HealthcareFacilityDetailView.as_view(),
        name="facility-detail",
    ),
]
