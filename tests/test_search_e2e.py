from datetime import date
from unittest.mock import patch

import pymupdf
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import override_settings

from archive.search_services import MedicalDocumentSearchService
from documents.date_services import confirm_document_date
from documents.services import create_medical_document, update_medical_document
from processing.date_services import process_date_candidates
from processing.services import process_pdf_document
from tests.archive_helpers import make_facility
from tests.test_date_processing import prepared_document
from tests.test_medical_document_services import actor_and_patient

pytestmark = [pytest.mark.django_db, pytest.mark.postgresql]


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only end-to-end search tests")


def synthetic_report_pdf():
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text(
        (60, 80),
        "Synthetic Medical Report - Report Date: 14/03/2026 - "
        "Hemoglobin level measured. " * 3,
    )
    content = pdf.tobytes()
    pdf.close()
    return content


def test_real_native_pdf_to_m13_search(tmp_path):
    require_postgresql()
    actor, patient = actor_and_patient()
    facility = make_facility()
    content = synthetic_report_pdf()
    upload = SimpleUploadedFile(
        "synthetic-report.pdf", content, content_type="application/pdf"
    )
    with (
        override_settings(MEDICAL_FILE_ROOT=tmp_path),
        patch("processing.tasks.extract_pdf_text.delay"),
        patch("processing.tasks.detect_document_dates.delay"),
    ):
        document = create_medical_document(
            patient=patient,
            actor=actor,
            upload=upload,
            metadata={"document_type": "OTHER", "title": "Synthetic Hemoglobin Report"},
        )
        original_uuid = document.uuid
        assert process_pdf_document(str(document.uuid)) == "TEXT_EXTRACTED"
        document.refresh_from_db()
        assert process_date_candidates(str(document.uuid)) == "AWAITING_CONFIRMATION"
        candidate = document.date_candidates.get(is_current=True, is_suggested=True)
        confirm_document_date(
            document=document, actor=actor, candidate_id=candidate.uuid
        )
        document = update_medical_document(
            document=document,
            actor=actor,
            metadata={
                "document_type": "LABORATORY",
                "healthcare_facility_id": facility.uuid,
            },
        )

        service = MedicalDocumentSearchService(patient)

        def uuids(filters):
            return {row.uuid for row in service.search_queryset(filters)[:50]}

        assert uuids({"q": "hemoglobin"}) == {original_uuid}
        assert uuids({"q": "synthetic"}) == {original_uuid}
        assert uuids({"year": 2026, "month": 3}) == {original_uuid}
        assert uuids({"document_type": "LABORATORY"}) == {original_uuid}
        assert uuids({"healthcare_facility": facility}) == {original_uuid}
        assert uuids({"year": 2026, "month": 4}) == set()


def test_real_ocr_canonical_arabic_text_search():
    require_postgresql()
    document = prepared_document("قيمة الهيموغلوبين ضمن المعدل الطبيعي")
    patient = document.patient
    actor = document.patient.user
    process_date_candidates(str(document.uuid))
    # No suggested date candidate is required for this search-focused flow:
    # confirm a manual authoritative date (M10) for the verified bucket.
    confirm_document_date(
        document=document,
        actor=actor,
        manual_date=date(2026, 3, 14),
    )
    service = MedicalDocumentSearchService(patient)
    rows = list(service.search_queryset({"q": "الهيموغلوبين"})[:50])
    assert [row.uuid for row in rows] == [document.uuid]
    # No raw OCR content leaks into a serialized search result.
    from archive.serializers import ArchiveDocumentSerializer

    encoded = str(ArchiveDocumentSerializer(rows, many=True).data)
    assert "الهيموغلوبين" not in encoded
