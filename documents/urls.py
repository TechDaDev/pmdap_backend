from django.urls import path

from documents.api import (
    MedicalDocumentCollectionView,
    MedicalDocumentDateCandidateView,
    MedicalDocumentDateConfirmationView,
    MedicalDocumentDetailView,
    MedicalDocumentFileView,
    MedicalDocumentPendingConfirmationView,
)

urlpatterns = [
    path("", MedicalDocumentCollectionView.as_view(), name="medical-document-list"),
    path(
        "date-confirmations/pending/",
        MedicalDocumentPendingConfirmationView.as_view(),
        name="medical-document-date-confirmations-pending",
    ),
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
    path(
        "<uuid:document_uuid>/confirm-date/",
        MedicalDocumentDateConfirmationView.as_view(),
        name="medical-document-confirm-date",
    ),
]
