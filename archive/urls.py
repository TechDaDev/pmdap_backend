from django.urls import path

from archive.api import ArchiveSummaryView, ArchiveView

urlpatterns = [
    path("", ArchiveView.as_view(), name="archive-list"),
    path("summary/", ArchiveSummaryView.as_view(), name="archive-summary"),
]
