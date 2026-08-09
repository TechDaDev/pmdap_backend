from unittest.mock import patch

import pymupdf
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from documents.date_services import confirm_document_date
from documents.services import create_medical_document, update_medical_document
from processing.date_services import process_date_candidates
from processing.services import process_pdf_document
from tests.test_m11_document_metadata import make_facility
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


def test_real_upload_to_text_date_confirmation_and_m11_metadata(tmp_path):
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
            metadata={"document_type": "OTHER", "facility_name": "Raw Facility"},
        )
        original_uuid = document.uuid
        original_file_id = document.stored_file_id
        original_digest = document.stored_file.sha256
        assert process_pdf_document(str(document.uuid)) == "TEXT_EXTRACTED"
        document.refresh_from_db()
        original_text = document.document_text.text
        assert process_date_candidates(str(document.uuid)) == "AWAITING_CONFIRMATION"
        candidate = document.date_candidates.get(is_current=True, is_suggested=True)
        document = confirm_document_date(
            document=document,
            actor=actor,
            candidate_id=candidate.uuid,
        )
        confirmed_date = document.document_date
        document = update_medical_document(
            document=document,
            actor=actor,
            metadata={
                "document_type": "MEDICAL_REPORT",
                "healthcare_facility_id": facility.uuid,
            },
        )

    document.refresh_from_db()
    assert document.uuid == original_uuid
    assert document.stored_file_id == original_file_id
    assert document.stored_file.sha256 == original_digest
    assert document.document_text.text == original_text
    assert document.document_date == confirmed_date
    assert document.date_verified is True
    assert document.document_type == "MEDICAL_REPORT"
    assert document.healthcare_facility_id == facility.uuid
