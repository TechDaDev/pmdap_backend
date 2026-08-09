from django.urls import path

from documents.api import (
    MedicalDocumentCollectionView,
    MedicalDocumentDateCandidateView,
    MedicalDocumentDetailView,
    MedicalDocumentFileView,
)

urlpatterns = [
    path("", MedicalDocumentCollectionView.as_view(), name="medical-document-list"),
    path(
        "<uuid:document_uuid>/",
        MedicalDocumentDetailView.as_view(),
        name="medical-document-detail",
    ),
    path(
        "<uuid:document_uuid>/file/",
        MedicalDocumentFileView.as_view(),
        name="medical-document-file",
    ),
    path(
        "<uuid:document_uuid>/date-candidates/",
        MedicalDocumentDateCandidateView.as_view(),
        name="medical-document-date-candidates",
    ),
]
