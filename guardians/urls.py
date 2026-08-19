from django.urls import path

from archive.api import MinorArchiveSummaryView, MinorArchiveView
from archive.search_api import MinorSearchView
from documents.api import (
    MinorExtractedContentView,
    MinorMedicalDocumentCollectionView,
    MinorMedicalDocumentDateCandidateView,
    MinorMedicalDocumentDateConfirmationView,
    MinorMedicalDocumentDetailView,
    MinorMedicalDocumentFileView,
    MinorMedicalDocumentPendingConfirmationView,
)
from guardians.api import MinorCollectionView, MinorDetailView
from labs.api import MinorLabResultsView

urlpatterns = [
    path("", MinorCollectionView.as_view(), name="minor-list-create"),
    path("<uuid:minor_uuid>/", MinorDetailView.as_view(), name="minor-detail"),
    path(
        "<uuid:minor_uuid>/archive/",
        MinorArchiveView.as_view(),
        name="minor-archive-list",
    ),
    path(
        "<uuid:minor_uuid>/archive/summary/",
        MinorArchiveSummaryView.as_view(),
        name="minor-archive-summary",
    ),
    path(
        "<uuid:minor_uuid>/search/",
        MinorSearchView.as_view(),
        name="minor-search",
    ),
    path(
        "<uuid:minor_uuid>/documents/",
        MinorMedicalDocumentCollectionView.as_view(),
        name="minor-medical-document-list",
    ),
    path(
        "<uuid:minor_uuid>/documents/<uuid:document_uuid>/",
        MinorMedicalDocumentDetailView.as_view(),
        name="minor-medical-document-detail",
    ),
    path(
        "<uuid:minor_uuid>/documents/<uuid:document_uuid>/file/",
        MinorMedicalDocumentFileView.as_view(),
        name="minor-medical-document-file",
    ),
    path(
        "<uuid:minor_uuid>/documents/<uuid:document_uuid>/lab-results/",
        MinorLabResultsView.as_view(),
        name="minor-medical-document-lab-results",
    ),
    path(
        "<uuid:minor_uuid>/documents/<uuid:document_uuid>/extracted-content/",
        MinorExtractedContentView.as_view(),
        name="minor-medical-document-extracted-content",
    ),
    path(
        "<uuid:minor_uuid>/documents/<uuid:document_uuid>/date-candidates/",
        MinorMedicalDocumentDateCandidateView.as_view(),
        name="minor-medical-document-date-candidates",
    ),
    path(
        "<uuid:minor_uuid>/documents/<uuid:document_uuid>/confirm-date/",
        MinorMedicalDocumentDateConfirmationView.as_view(),
        name="minor-medical-document-confirm-date",
    ),
    path(
        "<uuid:minor_uuid>/documents/date-confirmations/pending/",
        MinorMedicalDocumentPendingConfirmationView.as_view(),
        name="minor-medical-document-date-confirmations-pending",
    ),
]
