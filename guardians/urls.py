from django.urls import path

from guardians.api import MinorCollectionView, MinorDetailView

urlpatterns = [
    path("", MinorCollectionView.as_view(), name="minor-list-create"),
    path("<uuid:minor_uuid>/", MinorDetailView.as_view(), name="minor-detail"),
]
