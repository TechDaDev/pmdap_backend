import io
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from pypdf import PdfWriter

from accounts.models import User
from documents.models import MedicalDocument, MedicalDocumentEvent
from documents.services import create_medical_document
from patients.models import PatientProfile

pytestmark = pytest.mark.django_db


def actor_and_patient():
    actor = User.objects.create_user(
        email="dispatch-owner@example.com",
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    patient = PatientProfile.objects.create(
        user=actor,
        digital_id="12345678901234567",
        full_name="Synthetic Dispatch Owner",
        date_of_birth=date(1990, 1, 2),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )
    return actor, patient


def pdf_upload():
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(output)
    return SimpleUploadedFile(
        "report.pdf",
        output.getvalue(),
        content_type="application/pdf",
    )


def png_upload():
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(output, format="PNG")
    return SimpleUploadedFile(
        "scan.png",
        output.getvalue(),
        content_type="image/png",
    )


def create(patient, actor, upload):
    return create_medical_document(
        patient=patient,
        actor=actor,
        upload=upload,
        metadata={"document_type": MedicalDocument.DocumentType.MEDICAL_REPORT},
    )


def test_pdf_upload_queues_only_after_commit_without_sync_extraction(
    tmp_path,
    django_capture_on_commit_callbacks,
):
    actor, patient = actor_and_patient()
    with (
        override_settings(MEDICAL_FILE_ROOT=tmp_path),
        patch("processing.tasks.extract_pdf_text.delay") as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        document = create(patient, actor, pdf_upload())
        assert delay.call_count == 0

    assert document.processing_status == MedicalDocument.ProcessingStatus.QUEUED
    delay.assert_called_once_with(str(document.uuid))
    assert document.events.filter(
        event_type=MedicalDocumentEvent.EventType.PDF_EXTRACTION_QUEUED
    ).exists()


def test_png_upload_is_not_enqueued_or_mutated(
    tmp_path,
    django_capture_on_commit_callbacks,
):
    actor, patient = actor_and_patient()
    with (
        override_settings(MEDICAL_FILE_ROOT=tmp_path),
        patch("processing.tasks.extract_pdf_text.delay") as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        document = create(patient, actor, png_upload())

    assert document.processing_status == MedicalDocument.ProcessingStatus.UPLOADED
    delay.assert_not_called()
    assert not document.events.filter(
        event_type=MedicalDocumentEvent.EventType.PDF_EXTRACTION_QUEUED
    ).exists()


def test_broker_failure_after_commit_does_not_rollback_stored_document(
    tmp_path,
    django_capture_on_commit_callbacks,
):
    actor, patient = actor_and_patient()
    with (
        override_settings(MEDICAL_FILE_ROOT=tmp_path),
        patch(
            "processing.tasks.extract_pdf_text.delay",
            side_effect=OSError("broker unavailable"),
        ),
        django_capture_on_commit_callbacks(execute=True),
    ):
        document = create(patient, actor, pdf_upload())

    document.refresh_from_db()
    assert document.processing_status == MedicalDocument.ProcessingStatus.QUEUED
    assert Path(tmp_path, document.stored_file.file.name).exists()
