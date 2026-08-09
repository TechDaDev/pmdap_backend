import time
import uuid
from datetime import date, timedelta

import pytest
from django.db import connection

from archive.search_services import MedicalDocumentSearchService
from documents.models import MedicalDocument, StoredFile
from processing.models import DocumentText
from tests.archive_helpers import make_facility
from tests.test_medical_documents_api import patient_user

pytestmark = [pytest.mark.django_db, pytest.mark.postgresql]

SYNTHETIC_COUNT = 10_000
TITLES = [
    "CBC Result",
    "Blood Glucose Panel",
    "Chest XRay",
    "Cardiology Consultation",
    "Discharge Summary",
    "Vaccination Record",
    "Kidney Function Test",
    "Thyroid Panel",
    "Liver Function Test",
    "Surgery Procedure Note",
]
DESCRIPTIONS = [
    "Routine annual laboratory panel.",
    "Follow-up after medication change.",
    "Pre-operative assessment.",
    "Post-discharge review.",
]


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only search performance sanity")


def build_synthetic_search_archive(patient, uploaded_by, count=SYNTHETIC_COUNT):
    facilities = [make_facility(name=f"Bulk Facility {index}") for index in range(5)]
    document_types = list(MedicalDocument.DocumentType.values)
    stored = [
        StoredFile(
            file=f"medical/search-{uuid.uuid4().hex}.png",
            original_filename="search.png",
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
    documents = []
    for index in range(count):
        documents.append(
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
                facility_name="Bulk Raw Facility",
                department="Hematology" if index % 3 == 0 else "General",
                physician_name="Dr Synthetic",
                title=TITLES[index % len(TITLES)],
                description=DESCRIPTIONS[index % len(DESCRIPTIONS)],
                processing_status="DATE_CONFIRMED",
                archive_status=MedicalDocument.ArchiveStatus.ACTIVE,
            )
        )
    MedicalDocument.objects.bulk_create(documents)
    # Attach canonical text to a text-bearing subset.
    text_docs = MedicalDocument.objects.filter(patient=patient)[: count // 3]
    DocumentText.objects.bulk_create(
        [
            DocumentText(
                document=document,
                text=(
                    "Report text with hemoglobin value and "
                    f"{document.document_type.lower()}"
                ),
                page_count=1,
                character_count=60,
                meaningful_character_count=50,
                usable=True,
                usability_reason="usable_pdf_text",
                has_pages_requiring_ocr=False,
                extraction_method=DocumentText.ExtractionMethod.PDF_TEXT,
                extractor_name="PyMuPDF",
                extractor_version="1.28.0",
                pipeline_version="m7-v1",
            )
            for document in text_docs
        ]
    )
    return facilities


def timed(label, operation):
    started = time.perf_counter()
    result = operation()
    elapsed = time.perf_counter() - started
    print(f"\n[search-perf] {label}: {elapsed:.3f}s")
    return result, elapsed


def explain_plan(queryset):
    sql, params = queryset.query.sql_with_params()
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN {sql}", params)
        return "\n".join(row[0] for row in cursor.fetchall())


def test_synthetic_search_query_plan_and_timing():
    require_postgresql()
    user, patient = patient_user()
    facilities = build_synthetic_search_archive(patient, user)
    service = MedicalDocumentSearchService(patient)

    (kw, kw_time) = timed(
        "keyword", lambda: list(service.search_queryset({"q": "hemoglobin"})[:20])
    )
    assert len(kw) == 20
    kw_plan = explain_plan(service.search_queryset({"q": "hemoglobin"}))
    assert "Seq Scan" in kw_plan or "Index" in kw_plan

    (daterange, dr_time) = timed(
        "date range",
        lambda: list(
            service.search_queryset(
                {"date_from": "2020-01-01", "date_to": "2020-12-31"}
            )[:20]
        ),
    )
    assert len(daterange) == 20
    dr_plan = explain_plan(
        service.search_queryset({"date_from": "2020-01-01", "date_to": "2020-12-31"})
    )
    assert "Index" in dr_plan

    (typedate, td_time) = timed(
        "type + date",
        lambda: list(
            service.search_queryset({"year": 2020, "document_type": "LABORATORY"})[:20]
        ),
    )
    assert len(typedate) == 20

    (facdate, fd_time) = timed(
        "facility + date",
        lambda: list(
            service.search_queryset(
                {"year": 2020, "healthcare_facility": facilities[0]}
            )[:20]
        ),
    )
    assert len(facdate) == 20

    (kwfilter, kf_time) = timed(
        "keyword + type + year",
        lambda: list(
            service.search_queryset(
                {"q": "hemoglobin", "document_type": "LABORATORY", "year": 2020}
            )[:20]
        ),
    )
    assert len(kwfilter) <= 20

    assert max(kw_time, dr_time, td_time, fd_time, kf_time) < 15.0
