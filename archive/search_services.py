import logging
from datetime import UTC, date, datetime, time, timedelta

from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db.models import Q
from django.utils import timezone as dj_timezone

from documents.models import MedicalDocument

logger = logging.getLogger(__name__)

VERIFIED_ORDERING = ("-document_date", "-created_at", "-uuid")
UNCONFIRMED_ORDERING = ("-created_at", "-uuid")

# PostgreSQL text-search configuration. `simple` tokenizes on whitespace and
# punctuation without English stemming, so Arabic, English, and Kurdish tokens
# match lexically without false stemming. This is token search, not semantic
# multilingual search.
TEXT_SEARCH_CONFIG = "simple"


def _search_vector():
    return (
        SearchVector("title", weight="A", config=TEXT_SEARCH_CONFIG)
        + SearchVector("description", weight="B", config=TEXT_SEARCH_CONFIG)
        + SearchVector("facility_name", weight="B", config=TEXT_SEARCH_CONFIG)
        + SearchVector("location_text", weight="C", config=TEXT_SEARCH_CONFIG)
        + SearchVector("department", weight="C", config=TEXT_SEARCH_CONFIG)
        + SearchVector("physician_name", weight="C", config=TEXT_SEARCH_CONFIG)
        + SearchVector("document_text__text", weight="D", config=TEXT_SEARCH_CONFIG)
    )


class MedicalDocumentSearchService:
    """Patient-scoped search over active MedicalDocument records.

    Authorization is applied before any filter or keyword. Search is
    read-only and never mutates document metadata.
    """

    def __init__(self, patient):
        self.patient = patient

    def _active_queryset(self):
        return MedicalDocument.objects.filter(
            patient=self.patient,
            archive_status=MedicalDocument.ArchiveStatus.ACTIVE,
        )

    def search_queryset(self, filters):
        queryset = self._active_queryset().select_related(
            "healthcare_facility__country",
            "healthcare_facility__region",
            "healthcare_facility__city",
        )
        date_status = filters.get("date_status", "VERIFIED")
        if date_status == "UNCONFIRMED":
            queryset = queryset.filter(
                Q(date_verified=False) | Q(document_date__isnull=True)
            )
        else:
            queryset = queryset.filter(
                date_verified=True,
                document_date__isnull=False,
            )

        if document_type := filters.get("document_type"):
            queryset = queryset.filter(document_type=document_type)
        if facility := filters.get("healthcare_facility"):
            queryset = queryset.filter(healthcare_facility=facility)
        if department := filters.get("department"):
            queryset = queryset.filter(department__icontains=department)
        if physician := filters.get("physician_name"):
            queryset = queryset.filter(physician_name__icontains=physician)

        if date_from := filters.get("date_from"):
            queryset = queryset.filter(document_date__gte=date_from)
        if date_to := filters.get("date_to"):
            queryset = queryset.filter(document_date__lte=date_to)
        if year := filters.get("year"):
            queryset = queryset.filter(
                document_date__gte=date(year, 1, 1),
                document_date__lt=date(year + 1, 1, 1),
            )
        if month := filters.get("month"):
            start = date(year, month, 1)
            end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            queryset = queryset.filter(document_date__gte=start, document_date__lt=end)

        if uploaded_from := filters.get("uploaded_from"):
            queryset = queryset.filter(created_at__gte=self._day_start(uploaded_from))
        if uploaded_to := filters.get("uploaded_to"):
            queryset = queryset.filter(
                created_at__lt=self._day_start(uploaded_to + timedelta(days=1))
            )
        return queryset

    @staticmethod
    def _day_start(day):
        return dj_timezone.make_aware(datetime.combine(day, time.min), UTC)

    def with_keyword(self, queryset, keyword):
        vector = _search_vector()
        query = SearchQuery(keyword, config=TEXT_SEARCH_CONFIG)
        return queryset.annotate(
            search=vector,
            rank=SearchRank(vector, query),
        ).filter(search=query)

    def order(self, queryset, filters, *, has_keyword):
        if has_keyword:
            if filters.get("date_status") == "UNCONFIRMED":
                return queryset.order_by("-rank", *UNCONFIRMED_ORDERING)
            return queryset.order_by("-rank", *VERIFIED_ORDERING)
        if filters.get("date_status") == "UNCONFIRMED":
            return queryset.order_by(*UNCONFIRMED_ORDERING)
        return queryset.order_by(*VERIFIED_ORDERING)
