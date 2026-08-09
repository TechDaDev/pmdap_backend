from django.urls import path

from archive.search_api import SearchView

urlpatterns = [
    path("", SearchView.as_view(), name="search-list"),
]
