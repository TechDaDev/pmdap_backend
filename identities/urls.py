from django.urls import path

from identities.api import (
    IdentityDocumentCollectionView,
    IdentityDocumentDetailView,
    IdentityDocumentImageView,
    IdentityDocumentReplaceView,
    IdentityExtractionStatusView,
    IdentityExtractionView,
)

urlpatterns = [
    path("", IdentityDocumentCollectionView.as_view(), name="identity-document-list"),
    path(
        "extract/",
        IdentityExtractionView.as_view(),
        name="identity-document-extract",
    ),
    path(
        "extract/<uuid:job_uuid>/",
        IdentityExtractionStatusView.as_view(),
        name="identity-document-extract-status",
    ),
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
