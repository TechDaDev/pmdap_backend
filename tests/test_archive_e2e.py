from datetime import date
from unittest.mock import patch

import pymupdf
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from archive.services import ArchiveQueryService
from documents.date_services import confirm_document_date
from documents.models import MedicalDocument
from documents.services import create_medical_document, update_medical_document
from processing.date_services import process_date_candidates
from processing.services import process_pdf_document
from tests.archive_helpers import make_facility
from tests.test_medical_document_services import actor_and_patient

pytestmark = pytest.mark.django_db


def synthetic_report_pdf():
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text(
        (60, 80),
        "Synthetic Medical Report - Report Date: 14/03/2026 - " * 3,
    )
    content = pdf.tobytes()
    pdf.close()
    return content


def test_real_lifecycle_to_archive_and_date_correction_reposition(tmp_path):
    actor, patient = actor_and_patient()
    facility = make_facility()
    content = synthetic_report_pdf()
    upload = SimpleUploadedFile(
        "synthetic-report.pdf",
        content,
        content_type="application/pdf",
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
            metadata={
                "document_type": "OTHER",
                "facility_name": "Raw Facility",
            },
        )
        original_uuid = document.uuid
        original_file_id = document.stored_file_id
        original_digest = document.stored_file.sha256
        assert process_pdf_document(str(document.uuid)) == "TEXT_EXTRACTED"
        document.refresh_from_db()
        original_text = document.document_text.text
        assert process_date_candidates(str(document.uuid)) == "AWAITING_CONFIRMATION"
        candidate = document.date_candidates.get(is_current=True, is_suggested=True)
        confirmed = confirm_document_date(
            document=document,
            actor=actor,
            candidate_id=candidate.uuid,
        )
        confirmed_date = confirmed.document_date
        document = update_medical_document(
            document=document,
            actor=actor,
            metadata={
                "document_type": "LABORATORY",
                "healthcare_facility_id": facility.uuid,
            },
        )

        # --- M12 archive: correct year/month, type, facility, same identity ---
        service = ArchiveQueryService(patient)
        rows = list(
            service.chronological_queryset(
                {
                    "year": 2026,
                    "month": 3,
                    "document_type": "LABORATORY",
                    "healthcare_facility": facility,
                }
            )
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.uuid == original_uuid
        assert row.stored_file_id == original_file_id
        assert row.stored_file.sha256 == original_digest
        assert row.document_text.text == original_text
        assert row.document_date == confirmed_date
        assert row.date_verified is True
        assert row.document_type == "LABORATORY"
        assert row.healthcare_facility_id == facility.uuid
        assert row.facility_name == "Raw Facility"

        summary = service.summary()
        assert summary["years"][0]["year"] == 2026
        assert summary["years"][0]["count"] == 1
        assert summary["years"][0]["months"] == [{"month": 3, "count": 1}]
        types = {t["document_type"]: t["count"] for t in summary["document_types"]}
        assert types["LABORATORY"] == 1
        assert summary["unconfirmed_date_count"] == 0

        # --- M10 correction repositions the same document immediately ---
        corrected = confirm_document_date(
            document=document,
            actor=actor,
            manual_date=date(2026, 4, 2),
        )
        assert corrected.uuid == original_uuid
        assert corrected.stored_file_id == original_file_id
        assert corrected.stored_file.sha256 == original_digest
        assert corrected.document_date == date(2026, 4, 2)

        service = ArchiveQueryService(patient)
        assert service.chronological_queryset({"year": 2026, "month": 3}).count() == 0
        assert service.chronological_queryset({"year": 2026, "month": 4}).count() == 1
        assert (
            service.chronological_queryset(
                {"year": 2026, "month": 4, "document_type": "LABORATORY"}
            ).count()
            == 1
        )
        summary = service.summary()
        assert summary["years"][0]["months"] == [{"month": 4, "count": 1}]
        assert patient.medical_documents.count() == 1
        assert MedicalDocument.objects.count() == 1
