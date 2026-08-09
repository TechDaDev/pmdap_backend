import logging
from collections import defaultdict
from datetime import date

from django.db.models import Count, Q
from django.db.models.functions import ExtractMonth, ExtractYear

from documents.models import MedicalDocument

logger = logging.getLogger(__name__)

VERIFIED_ORDERING = ("-document_date", "-created_at", "-uuid")
UNCONFIRMED_ORDERING = ("-created_at", "-uuid")


class ArchiveQueryService:
    """Metadata-driven archive views over active MedicalDocument records.

    The archive is a query projection, never a duplicate source of truth. This
    service owns authorization-bounded queryset construction, filtering,
    deterministic ordering, and grouping so views stay thin.
    """

    def __init__(self, patient):
        self.patient = patient

    def _active_queryset(self):
        return MedicalDocument.objects.filter(
            patient=self.patient,
            archive_status=MedicalDocument.ArchiveStatus.ACTIVE,
        )

    def archive_queryset(self):
        return self._active_queryset().select_related(
            "healthcare_facility__country",
            "healthcare_facility__region",
            "healthcare_facility__city",
        )

    def chronological_queryset(self, filters):
        queryset = self.archive_queryset().filter(
            date_verified=True,
            document_date__isnull=False,
        )
        return self._apply_verified_filters(queryset, filters)

    def unconfirmed_queryset(self, filters):
        queryset = self.archive_queryset().filter(
            Q(date_verified=False) | Q(document_date__isnull=True)
        )
        if document_type := filters.get("document_type"):
            queryset = queryset.filter(document_type=document_type)
        if facility := filters.get("healthcare_facility"):
            queryset = queryset.filter(healthcare_facility=facility)
        return queryset

    def unconfirmed_count(self):
        return (
            self._active_queryset()
            .filter(Q(date_verified=False) | Q(document_date__isnull=True))
            .count()
        )

    def _apply_verified_filters(self, queryset, filters):
        if document_type := filters.get("document_type"):
            queryset = queryset.filter(document_type=document_type)
        if facility := filters.get("healthcare_facility"):
            queryset = queryset.filter(healthcare_facility=facility)
        if year := filters.get("year"):
            queryset = queryset.filter(
                document_date__gte=date(year, 1, 1),
                document_date__lt=date(year + 1, 1, 1),
            )
        if month := filters.get("month"):
            start = date(year, month, 1)
            end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            queryset = queryset.filter(
                document_date__gte=start,
                document_date__lt=end,
            )
        return queryset

    def summary(self):
        active = self._active_queryset()
        verified = active.filter(
            date_verified=True,
            document_date__isnull=False,
        )

        year_rows = (
            verified.annotate(year=ExtractYear("document_date"))
            .values("year")
            .annotate(count=Count("pk"))
            .order_by("-year")
        )
        month_rows = (
            verified.annotate(
                year=ExtractYear("document_date"),
                month=ExtractMonth("document_date"),
            )
            .values("year", "month")
            .annotate(count=Count("pk"))
            .order_by("-year", "-month")
        )
        months_by_year = defaultdict(dict)
        for row in month_rows:
            months_by_year[row["year"]][row["month"]] = row["count"]
        years = [
            {
                "year": row["year"],
                "count": row["count"],
                "months": [
                    {"month": month, "count": count}
                    for month, count in sorted(
                        months_by_year[row["year"]].items(), reverse=True
                    )
                ],
            }
            for row in year_rows
        ]

        type_rows = (
            active.values("document_type")
            .annotate(count=Count("pk"))
            .order_by("-count", "document_type")
        )
        facility_rows = (
            active.exclude(healthcare_facility__isnull=True)
            .values("healthcare_facility__uuid", "healthcare_facility__name")
            .annotate(count=Count("pk"))
            .order_by("-count", "healthcare_facility__name")
        )
        return {
            "years": years,
            "document_types": [
                {"document_type": row["document_type"], "count": row["count"]}
                for row in type_rows
            ],
            "facilities": [
                {
                    "uuid": str(row["healthcare_facility__uuid"]),
                    "name": row["healthcare_facility__name"],
                    "count": row["count"],
                }
                for row in facility_rows
            ],
            "unconfirmed_date_count": self.unconfirmed_count(),
        }
