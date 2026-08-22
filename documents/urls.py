from django.urls import path

from documents.api import (
    ExtractedContentView,
    MedicalDocumentCollectionView,
    MedicalDocumentDateCandidateView,
    MedicalDocumentDateConfirmationView,
    MedicalDocumentDetailView,
    MedicalDocumentFileView,
    MedicalDocumentPageDateConfirmationView,
    MedicalDocumentPageDetailView,
    MedicalDocumentPageLabResultsView,
    MedicalDocumentPageListView,
    MedicalDocumentPendingConfirmationView,
)
from labs.api import LabResultsView

urlpatterns = [
    path("", MedicalDocumentCollectionView.as_view(), name="medical-document-list"),
    path(
        "date-confirmations/pending/",
        MedicalDocumentPendingConfirmationView.as_view(),
        name="medical-document-date-confirmations-pending",
    ),
    path(
        "<uuid:document_uuid>/pages/",
        MedicalDocumentPageListView.as_view(),
        name="medical-document-pages",
    ),
    path(
        "<uuid:document_uuid>/pages/<int:page_number>/lab-results/",
        MedicalDocumentPageLabResultsView.as_view(),
        name="medical-document-page-lab-results",
    ),
    path(
        "<uuid:document_uuid>/pages/<int:page_number>/confirm-date/",
        MedicalDocumentPageDateConfirmationView.as_view(),
        name="medical-document-page-confirm-date",
    ),
    path(
        "<uuid:document_uuid>/pages/<int:page_number>/",
        MedicalDocumentPageDetailView.as_view(),
        name="medical-document-page-detail",
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
        "<uuid:document_uuid>/lab-results/",
        LabResultsView.as_view(),
        name="medical-document-lab-results",
    ),
    path(
        "<uuid:document_uuid>/extracted-content/",
        ExtractedContentView.as_view(),
        name="medical-document-extracted-content",
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
