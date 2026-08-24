import logging
from collections import defaultdict
from datetime import date

from django.db.models import Case, Count, F, DateTimeField, When
from django.db.models.functions import ExtractMonth, ExtractYear

from documents.date_services import pending_confirmation_queryset
from documents.models import MedicalDocument
from documents.page_services import pending_page_units

logger = logging.getLogger(__name__)

# Undated documents sort by creation time; dated ones by their confirmed date.
EFFECTIVE_SORT_DATE = Case(
    When(
        date_verified=True,
        document_date__isnull=False,
        then=F("document_date"),
    ),
    default=F("created_at"),
    output_field=DateTimeField(),
)
VERIFIED_ORDERING = ("-sort_date", "-created_at", "-uuid")
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
            "stored_file",
        )

    def chronological_queryset(self, filters):
        # Archive = every active document, INCLUDING undated / date-unconfirmed
        # ones (their report date may still need confirmation). A year/month
        # filter naturally keeps only documents with a matching confirmed
        # report date; "All dates" shows everything.
        queryset = self.archive_queryset().annotate(sort_date=EFFECTIVE_SORT_DATE)
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
        return queryset.order_by(*VERIFIED_ORDERING)

    def unconfirmed_queryset(self, filters):
        # Distinct documents that have at least one pending report-page unit
        # (same authoritative rule as the confirm-dates queue). Archive still
        # shows ONE card per source document even for multi-page PDFs.
        pending_ids = pending_page_units(self.patient).values("document_id")
        queryset = MedicalDocument.objects.filter(
            pk__in=pending_ids,
            archive_status=MedicalDocument.ArchiveStatus.ACTIVE,
        ).select_related(
            "healthcare_facility__country",
            "healthcare_facility__region",
            "healthcare_facility__city",
            "stored_file",
        )
        if document_type := filters.get("document_type"):
            queryset = queryset.filter(document_type=document_type)
        if facility := filters.get("healthcare_facility"):
            queryset = queryset.filter(healthcare_facility=facility)
        return queryset.order_by(*UNCONFIRMED_ORDERING)

    def unconfirmed_count(self):
        # Page units, not source documents — a 3-page PDF contributes 3 so the
        # Home badge, queue page, and archive count can never drift.
        return pending_page_units(self.patient).count()

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
