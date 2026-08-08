from django.urls import path

from identities.api import (
    IdentityDocumentCollectionView,
    IdentityDocumentDetailView,
    IdentityDocumentImageView,
    IdentityDocumentReplaceView,
)

urlpatterns = [
    path("", IdentityDocumentCollectionView.as_view(), name="identity-document-list"),
    path(
        "<uuid:document_uuid>/",
        IdentityDocumentDetailView.as_view(),
        name="identity-document-detail",
    ),
    path(
        "<uuid:document_uuid>/replace/",
        IdentityDocumentReplaceView.as_view(),
        name="identity-document-replace",
    ),
    path(
        "<uuid:document_uuid>/images/<str:side>/",
        IdentityDocumentImageView.as_view(),
        name="identity-document-image",
    ),
]
