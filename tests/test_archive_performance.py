import time
import uuid
from datetime import date, timedelta

import pytest
from django.db import connection

from archive.services import ArchiveQueryService
from documents.models import MedicalDocument, StoredFile
from tests.archive_helpers import make_facility
from tests.test_medical_documents_api import patient_user

pytestmark = [pytest.mark.django_db, pytest.mark.postgresql]

SYNTHETIC_COUNT = 1500


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only archive performance sanity")


def build_synthetic_archive(patient, uploaded_by, count=SYNTHETIC_COUNT):
    facilities = [make_facility(name=f"Bulk Facility {index}") for index in range(5)]
    document_types = list(MedicalDocument.DocumentType.values)
    stored = [
        StoredFile(
            file=f"medical/bulk-{uuid.uuid4().hex}.png",
            original_filename="bulk.png",
            mime_type="image/png",
            size_bytes=4,
            sha256=uuid.uuid4().hex * 2,
            page_count=1,
            integrity_status=StoredFile.IntegrityStatus.VALID,
            malware_scan_status=StoredFile.MalwareScanStatus.CLEAN,
        )
        for _ in range(count)
    ]
    StoredFile.objects.bulk_create(stored)
    base = date(2018, 1, 1)
    documents = [
        MedicalDocument(
            patient=patient,
            uploaded_by=uploaded_by,
            stored_file=stored[index],
            content_sha256=uuid.uuid4().hex,
            document_type=document_types[index % len(document_types)],
            document_date=base + timedelta(days=index),
            date_verified=True,
            date_source="USER_CONFIRMED",
            healthcare_facility=facilities[index % len(facilities)],
            facility_name="Bulk Raw",
            processing_status="DATE_CONFIRMED",
            archive_status=MedicalDocument.ArchiveStatus.ACTIVE,
        )
        for index in range(count)
    ]
    MedicalDocument.objects.bulk_create(documents)
    return facilities, document_types


def timed(label, operation):
    started = time.perf_counter()
    result = operation()
    elapsed = time.perf_counter() - started
    print(f"\n[archive-perf] {label}: {elapsed:.3f}s")
    return result, elapsed


def explain_plan(queryset):
    sql, params = queryset.query.sql_with_params()
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN {sql}", params)
        return "\n".join(row[0] for row in cursor.fetchall())


def test_synthetic_archive_query_plan_and_timing(api_client):
    require_postgresql()
    user, patient = patient_user()
    facilities, document_types = build_synthetic_archive(patient, user)
    service = ArchiveQueryService(patient)

    chrono, chrono_time = timed(
        "default chronology", lambda: list(service.chronological_queryset({})[:20])
    )
    assert len(chrono) == 20
    plan = explain_plan(service.chronological_queryset({}))
    assert "Index" in plan

    year, year_time = timed(
        "year filter", lambda: list(service.chronological_queryset({"year": 2019}))
    )
    assert len(year) == 365

    year_month, ym_time = timed(
        "year+month",
        lambda: list(service.chronological_queryset({"year": 2020, "month": 6})),
    )
    assert len(year_month) == 30

    doc_type, type_time = timed(
        "document type",
        lambda: list(
            service.chronological_queryset({"document_type": document_types[0]})
        ),
    )
    assert len(doc_type) == SYNTHETIC_COUNT // len(document_types)

    facility, facility_time = timed(
        "facility",
        lambda: list(
            service.chronological_queryset({"healthcare_facility": facilities[0]})
        ),
    )
    assert len(facility) == SYNTHETIC_COUNT // len(facilities)

    summary, summary_time = timed("summary", service.summary)
    assert sum(row["count"] for row in summary["years"]) == SYNTHETIC_COUNT
    assert sum(row["count"] for row in summary["document_types"]) == SYNTHETIC_COUNT
    assert sum(row["count"] for row in summary["facilities"]) == SYNTHETIC_COUNT
    assert summary["unconfirmed_date_count"] == 0

    # Loose guard: each representative query stays bounded on a dev machine.
    assert (
        max(chrono_time, year_time, ym_time, type_time, facility_time, summary_time)
        < 10.0
    )
